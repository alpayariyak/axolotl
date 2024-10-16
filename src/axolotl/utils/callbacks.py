"""Callbacks for Trainer class"""

from __future__ import annotations
import time
import logging
import os
from typing import TYPE_CHECKING, Dict, List
import runpod.serverless
import evaluate
import json
import numpy as np
import pandas as pd
import torch
import torch.distributed as dist
import wandb
from datasets import load_dataset
from optimum.bettertransformer import BetterTransformer
from tqdm import tqdm
from transformers import (
    GenerationConfig,
    Trainer,
    TrainerCallback,
    TrainerControl,
    TrainerState,
    TrainingArguments,
)
from transformers.trainer_utils import PREFIX_CHECKPOINT_DIR, IntervalStrategy

from axolotl.utils.bench import log_gpu_memory_usage
from axolotl.utils.distributed import (
    barrier,
    broadcast_dict,
    gather_scalar_from_all_ranks,
    get_world_size,
    is_distributed,
    is_main_process,
    zero_first,
)

if TYPE_CHECKING:
    from axolotl.core.trainer_builder import AxolotlTrainingArguments

LOG = logging.getLogger("axolotl.callbacks")
IGNORE_INDEX = -100


def rewrite_logs(d):
    new_d = {'train': {}, 'eval': {}, 'test': {}}
    eval_prefix = "eval_"
    test_prefix = "test_"
    for k, v in d.items():
        if k.startswith(eval_prefix):
            new_d['eval'][k[len(eval_prefix):]] = v
        elif k.startswith(test_prefix):
            new_d['test'][k[len(test_prefix):]] = v
        else:
            new_d['train'][k] = v
    return new_d


class RunPodCallback(TrainerCallback):
    """
    A `TrainerCallback` that sends logs and progress updates to RunPod.
    Logs are sent in JSON format with different keys for different types of information.
    """

    def __init__(self, job_id, verbose=False):
        """
        Initialize the RunPodCallback with the job ID for RunPod updates.
        """
        self.last_logged_step = None
        self.wandb_run_url = None
        self.job_id = {"id": job_id}
        self.total_tracked_steps = None
        self.current_tracked_steps = 0
        self.verbose = verbose
        self.training_start_time = time.time()
        self.total_eval_time = 0
        self.last_log_time = time.time()
        self.metrics = {}
        self.logger = runpod.RunPodLogger()

    def _send_update(self, message_content):
        """
        Sends a structured JSON message to RunPod.
        """
        message = json.dumps(message_content)
        self.logger.info(message, job_id=self.job_id)
        runpod.serverless.progress_update(self.job_id, message)

    def on_log(self, args, state, control, logs=None, **kwargs):
        """
        Called after any `Trainer` logs are created.
        """

        if state.is_world_process_zero:
            formatted_logs = rewrite_logs(logs)
            progress_content = {
                "train": formatted_logs['train'],
                "eval": formatted_logs['eval'],
                "test": formatted_logs['test']
            }
            self.metrics = progress_content

    def on_train_begin(self, args, state, control, **kwargs):
        """
        Called at the beginning of training, before any steps have occurred.
        """
        if wandb.run:
            self.wandb_run_url = wandb.run.get_url()
        if state.is_world_process_zero:
            self.training_start_time = time.time()
            self.total_tracked_steps = kwargs.get("total_num_training_steps", state.max_steps)
            self.last_logged_step = state.global_step

    def on_step_end(self, args, state, control, **kwargs):
        # Increment the step count for training steps
        self.current_tracked_steps += 1

        # Calculate the elapsed training time excluding evaluation time
        current_time = time.time()
        training_time_elapsed = current_time - self.training_start_time - self.total_eval_time

        # If it's time to log (based on your logging strategy)
        if state.global_step % args.logging_steps == 0:
            # Calculate time elapsed and remaining
            time_per_step = training_time_elapsed / self.current_tracked_steps
            time_remaining = time_per_step * (self.total_tracked_steps - self.current_tracked_steps)
            progress_content = {
                "step": state.global_step,
                "total_steps": self.total_tracked_steps,
                "time_seconds": {
                    "elapsed": training_time_elapsed,
                    "remaining": time_remaining
                },
                "metrics": self.metrics,
                "wandb": self.wandb_run_url,
            }
            self._send_update(progress_content)
            # Update the last log time
            self.last_log_time = current_time

    def on_evaluate(self, args, state, control, **kwargs):
        # Evaluation completed, update the total evaluation time
        self.total_eval_time += time.time() - self.last_log_time

    # def on_train_end(self, args, state, control, **kwargs):
    #     """
    #     Called at the end of training.
    #     """
    #     if state.is_world_process_zero:
    #         self._send_update("status", "Training completed.")


class EvalFirstStepCallback(
    TrainerCallback
):  # pylint: disable=too-few-public-methods disable=unused-argument
    """
    Callback to trigger evals on the first step
    """

    def on_step_end(
        self,
        args: TrainingArguments,
        state: TrainerState,
        control: TrainerControl,
        **kwargs,
    ):
        if (
            args.evaluation_strategy == IntervalStrategy.STEPS
            and args.eval_steps < 1.0
            and state.global_step == 1
        ):
            control.should_evaluate = True
        return control


class SaveBetterTransformerModelCallback(
    TrainerCallback
):  # pylint: disable=too-few-public-methods
    """Callback to save the BetterTransformer wrapped model"""

    def on_step_end(
        self,
        args: TrainingArguments,
        state: TrainerState,
        control: TrainerControl,
        **kwargs,
    ):
        # Save
        if (
            args.save_strategy == IntervalStrategy.STEPS
            and args.save_steps > 0
            and state.global_step % args.save_steps == 0
        ):
            control.should_save = True

        if control.should_save:
            checkpoint_folder = os.path.join(
                args.output_dir,
                f"{PREFIX_CHECKPOINT_DIR}-{state.global_step}",
            )

            model = BetterTransformer.reverse(kwargs["model"])
            model.save_pretrained(checkpoint_folder)
            # FIXME - need to cleanup old checkpoints

            # since we're saving here, we don't need the trainer loop to attempt to save too b/c
            # the trainer will raise an exception since it can't save a BetterTransformer wrapped model
            control.should_save = False
        return control


class GPUStatsCallback(
    TrainerCallback
):  # pylint: disable=too-few-public-methods disable=unused-argument
    """Callback to track GPU utilization"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.logged = False

    def on_step_end(
        self,
        args: TrainingArguments,
        state: TrainerState,
        control: TrainerControl,
        **kwargs,
    ):
        if not self.logged and state.global_step > 1:
            log_gpu_memory_usage(LOG, "while training", self.cfg.device)
            self.logged = True
        return control


def bench_eval_callback_factory(trainer, tokenizer):
    accuracy = evaluate.load("accuracy")
    abcd_idx = [
        tokenizer("A", add_special_tokens=False).input_ids[0],
        tokenizer("B", add_special_tokens=False).input_ids[0],
        tokenizer("C", add_special_tokens=False).input_ids[0],
        tokenizer("D", add_special_tokens=False).input_ids[0],
        tokenizer("E", add_special_tokens=False).input_ids[0],
        tokenizer("F", add_special_tokens=False).input_ids[0],
        tokenizer("G", add_special_tokens=False).input_ids[0],
    ]
    bench_split = "eval"

    def transform_bench_subject(example):
        # Split on ':' and trim whitespace
        parts = example["subject"].split(":")
        first_part = (
            parts[0].strip().lower().replace("-", "_")
        )  # Lowercase the first part
        second_part = (
            parts[1].strip().replace("-", "_") if len(parts) > 1 else "all"
        )  # Replace hyphens with underscores

        # Return the transformed values
        return {"name": first_part, "subject": second_part}

    if trainer.args.bench_dataset == "mmlu-zs":
        bench_dataset = load_dataset(
            "openaccess-ai-collective/mmlu-evals",
            data_files={
                "eval": "zero_shot_mmlu_val.json",
                "test": "zero_shot_mmlu_test.json",
            },
        )
        # bench_dataset = bench_dataset.remove_columns("subject")
    # MMLU Five-shot (Eval/Test only)
    elif trainer.args.bench_dataset in ["mmlu", "mmlu-fs"]:
        bench_dataset = load_dataset(
            "openaccess-ai-collective/mmlu-evals",
            data_files={
                "eval": "five_shot_mmlu_val.json",
                "test": "five_shot_mmlu_test.json",
            },
        )
        # bench_dataset = bench_dataset.remove_columns('subject')
    elif "/" in trainer.args.bench_dataset:
        bench_ds = trainer.args.bench_dataset
        bench_ds_name = "/".join(bench_ds.split("/", 2)[:2])
        bench_ds_data_file = "/".join(bench_ds.split("/", 2)[2:])
        bench_dataset = load_dataset(
            bench_ds_name,
            data_files={
                "eval": bench_ds_data_file,
            },
        )
        bench_dataset["eval"] = bench_dataset["eval"].map(transform_bench_subject)
    else:
        raise ValueError(
            f"unhandled value `{trainer.args.bench_dataset}` for bench_dataset training args"
        )
    bench_dataset = bench_dataset[trainer.args.bench_split]
    if trainer.args.max_bench_samples is not None:
        bench_dataset = bench_dataset.select(range(trainer.args.max_bench_samples))

    def tokenize_evals(example):
        source = f"{tokenizer.bos_token}{example['input']}"
        target = f"{example['output']}{tokenizer.eos_token}"

        tokenized_source = tokenizer(
            source,
            max_length=2048,
            truncation=True,
            add_special_tokens=False,
        )
        tokenized_target = tokenizer(
            target,
            max_length=2048,
            truncation=True,
            add_special_tokens=False,
        )
        input_ids = tokenized_source["input_ids"] + tokenized_target["input_ids"]
        labels = [IGNORE_INDEX] * len(tokenized_source["input_ids"]) + tokenized_target[
            "input_ids"
        ]

        return {
            "input_ids": input_ids,
            "labels": labels,
            "subject": example["subject"],
        }

    with zero_first(is_main_process()):
        bench_dataset = bench_dataset.map(tokenize_evals)
        bench_dataset = bench_dataset.filter(lambda x: x["labels"][-2] in abcd_idx)

    class BenchEvalCallback(TrainerCallback):
        """
        TrainerCallback that runs the MMLU evals
        """

        def on_evaluate(
            self,
            args: AxolotlTrainingArguments,
            state: TrainerState,  # pylint: disable=unused-argument
            control: TrainerControl,  # pylint: disable=unused-argument
            metrics: Dict[str, float],  # pylint: disable=unused-argument
            **kwargs,  # pylint: disable=unused-argument
        ):
            data_loader = trainer.get_bench_dataloader(
                bench_dataset.remove_columns(["input", "subject", "output", "name"])
            )
            trainer.model.eval()
            preds, refs = [], []
            loss_bench = 0
            for batch in tqdm(data_loader, total=len(data_loader)):
                (loss, logits, labels) = trainer.prediction_step(
                    trainer.model,
                    batch,
                    prediction_loss_only=False,
                )
                # There are two tokens, the output, and eos token.
                for i, logit in enumerate(logits):
                    label_non_zero_id = (batch["labels"][i] != IGNORE_INDEX).nonzero()[
                        0
                    ][0]
                    logit_abcd = logit[label_non_zero_id - 1][abcd_idx]
                    preds.append(torch.argmax(logit_abcd).item())
                labels = labels[labels != IGNORE_INDEX].view(-1, 2)[:, 0]
                refs += [
                    abcd_idx.index(label) if label in abcd_idx else -1
                    for label in labels.tolist()
                ]
                loss_bench += loss.item()
            # Extract results by subject.
            bench_name = bench_dataset["name"]
            bench_names: dict = {s: {"refs": [], "preds": []} for s in set(bench_name)}
            for s, p, r in zip(bench_name, preds, refs):  # pylint: disable=invalid-name
                bench_names[s]["preds"].append(p)
                bench_names[s]["refs"].append(r)
            barrier()
            local_bench_names = bench_names
            gathered_bench_names: List[Dict] = [{} for _ in range(get_world_size())]
            # Gather results from all GPUs to GPU 0

            loss_bench_ranks = gather_scalar_from_all_ranks(
                lambda: loss_bench, get_world_size()
            )
            len_data_loader_ranks = gather_scalar_from_all_ranks(
                lambda: len(data_loader), get_world_size()
            )

            results = {}
            if is_distributed() and not is_main_process():
                dist.gather_object(local_bench_names, dst=0)
            else:
                if is_distributed():
                    dist.gather_object(local_bench_names, gathered_bench_names, dst=0)
                else:
                    gathered_bench_names = [local_bench_names]
                bench_loss = sum(loss_bench_ranks) / sum(len_data_loader_ranks)
                results = {f"{bench_split}_bench_loss": bench_loss}

                # Combine results from all GPUs
                combined_bench_names: Dict[str, Dict[str, List]] = {}
                for bench_name in gathered_bench_names:
                    for name, data in bench_name.items():
                        if name not in combined_bench_names:
                            combined_bench_names[name] = {"refs": [], "preds": []}
                        combined_bench_names[name]["refs"].extend(data["refs"])
                        combined_bench_names[name]["preds"].extend(data["preds"])

                bench_scores = []
                bench_refs = []
                bench_preds = []
                for (
                    bench_name
                ) in combined_bench_names:  # pylint: disable=consider-using-dict-items
                    bench_score = accuracy.compute(
                        references=combined_bench_names[bench_name]["refs"],
                        predictions=combined_bench_names[bench_name]["preds"],
                    )["accuracy"]
                    bench_refs.extend(combined_bench_names[bench_name]["refs"])
                    bench_preds.extend(combined_bench_names[bench_name]["preds"])
                    if not pd.isna(bench_score):
                        results[
                            f"{bench_split}_bench_accuracy_{bench_name}"
                        ] = bench_score
                        bench_scores.append(bench_score)
                    else:
                        results[f"{bench_split}_bench_accuracy_{bench_name}"] = 0.0
                        bench_scores.append(0.0)
                results[f"{bench_split}_bench_average_accuracy"] = np.mean(bench_scores)
                results[f"{bench_split}_bench_total_accuracy"] = accuracy.compute(
                    references=bench_refs, predictions=bench_preds
                )["accuracy"]
                trainer.log(results)

            results = broadcast_dict(results)
            for key, val in results.items():
                metrics[key] = val

    return BenchEvalCallback


def log_prediction_callback_factory(trainer: Trainer, tokenizer):
    class LogPredictionCallback(TrainerCallback):
        """Callback to log prediction values during each evaluation"""

        def __init__(self, cfg):
            self.cfg = cfg
            self.logged = False

        def on_evaluate(
            self,
            args: AxolotlTrainingArguments,  # pylint: disable=unused-argument
            state: TrainerState,
            control: TrainerControl,
            train_dataloader,  # pylint: disable=unused-argument
            eval_dataloader,
            **kwargs,  # pylint: disable=unused-argument
        ):
            eval_table_size = self.cfg.eval_table_size

            if eval_table_size <= 0:
                return control

            trainer.model.eval()
            device = torch.device(self.cfg.device)

            # pylint: disable=duplicate-code
            generation_config = GenerationConfig(
                max_new_tokens=self.cfg.eval_table_max_new_tokens,
                bos_token_id=tokenizer.bos_token_id,
                eos_token_id=tokenizer.eos_token_id,
                pad_token_id=tokenizer.pad_token_id,
                do_sample=False,
                use_cache=True,
                return_dict_in_generate=True,
                output_attentions=False,
                output_hidden_states=False,
                output_scores=False,
            )

            def logits_to_tokens(logits) -> torch.Tensor:
                probabilities = torch.softmax(logits, dim=-1)
                # Get the predicted token ids (the ones with the highest probability)
                predicted_token_ids = torch.argmax(probabilities, dim=-1)
                return predicted_token_ids

            def find_ranges(lst):
                ranges = []
                start = 0
                for i in range(1, len(lst)):
                    if lst[i] == 0:
                        ranges.append((start, i - 1))
                        start = i
                end = len(lst) - 1
                ranges.append((start, end))
                return ranges

            def log_table_from_dataloader(name: str, table_dataloader):
                table = wandb.Table(  # type: ignore[attr-defined]
                    columns=[
                        "id",
                        "Prompt",
                        "Correct Completion",
                        "Predicted Completion (model.generate)",
                        "Predicted Completion (trainer.prediction_step)",
                    ]
                )
                row_index = 0

                for batch in tqdm(table_dataloader):
                    if row_index > eval_table_size:
                        break

                    batch_labels = batch["labels"].to(device)
                    batch_input_ids = batch["input_ids"].to(device)

                    if "position_ids" in batch:
                        batch_pos_ids = batch["position_ids"].tolist()
                    else:
                        batch_pos_ids = [None] * len(batch["input_ids"])

                    (_, batch_logits, _) = trainer.prediction_step(
                        trainer.model,
                        batch,
                        prediction_loss_only=False,
                    )

                    prompt_token_ids_list = []
                    pred_step_token_ids_list = []
                    completion_token_ids_list = []

                    for input_ids_all, labels_all, pos_ids, logits in zip(
                        batch_input_ids,
                        batch_labels,
                        batch_pos_ids,
                        batch_logits,
                    ):
                        if pos_ids is None:
                            pos_ranges = [(0, len(input_ids_all) - 1)]
                        else:
                            pos_ranges = find_ranges(pos_ids)

                        for pos_range in pos_ranges:
                            start, end = pos_range
                            if start == end:
                                continue

                            input_ids = input_ids_all[start: end + 1]
                            labels = labels_all[start: end + 1]

                            tokens_without_loss = labels == IGNORE_INDEX
                            tokens_with_loss = labels != IGNORE_INDEX
                            tokens_exclude_padding = input_ids != tokenizer.pad_token_id
                            prompt_token_includes = (
                                tokens_without_loss & tokens_exclude_padding
                            )

                            prompt_token_ids = input_ids[prompt_token_includes]
                            prompt_token_ids_list.append(prompt_token_ids)

                            completion_token_ids = input_ids[tokens_with_loss]
                            completion_token_ids_list.append(completion_token_ids)

                            pred_step_token_ids = logits_to_tokens(
                                logits[start: end + 1]
                            )[tokens_with_loss]
                            pred_step_token_ids_list.append(pred_step_token_ids)

                    prompt_texts = tokenizer.batch_decode(
                        prompt_token_ids_list, skip_special_tokens=True
                    )
                    completion_texts = tokenizer.batch_decode(
                        completion_token_ids_list, skip_special_tokens=True
                    )
                    pred_step_texts = tokenizer.batch_decode(
                        pred_step_token_ids_list, skip_special_tokens=True
                    )

                    with torch.no_grad():
                        prompt_encoding = tokenizer(
                            prompt_texts, padding=True, return_tensors="pt"
                        ).to(self.cfg.device)
                        predictions = trainer.model.generate(
                            **prompt_encoding, generation_config=generation_config
                        )

                    prediction_all_tokens = predictions["sequences"].cpu().tolist()
                    prediction_without_prompt_tokens_list = []
                    for prompt_token_ids, prediction_tokens in zip(
                        prompt_token_ids_list, prediction_all_tokens
                    ):
                        prediction_without_prompt_tokens = prediction_tokens[
                                                           len(prompt_token_ids):
                                                           ]
                        prediction_without_prompt_tokens_list.append(
                            prediction_without_prompt_tokens
                        )

                    predicted_texts = tokenizer.batch_decode(
                        prediction_without_prompt_tokens_list, skip_special_tokens=True
                    )

                    for (
                        prompt_text,
                        completion_text,
                        prediction_text,
                        pred_step_text,
                    ) in zip(
                        prompt_texts, completion_texts, predicted_texts, pred_step_texts
                    ):
                        table.add_data(
                            row_index,
                            prompt_text,
                            completion_text,
                            prediction_text,
                            pred_step_text,
                        )
                        row_index += 1

                wandb.run.log({f"{name} - Predictions vs Ground Truth": table})  # type: ignore[attr-defined]

            if is_main_process():
                log_table_from_dataloader("Eval", eval_dataloader)

            return control

    return LogPredictionCallback


class SaveAxolotlConfigtoWandBCallback(TrainerCallback):
    """Callback to save axolotl config to wandb"""

    def __init__(self, axolotl_config_path):
        self.axolotl_config_path = axolotl_config_path

    def on_train_begin(
        self,
        args: AxolotlTrainingArguments,  # pylint: disable=unused-argument
        state: TrainerState,  # pylint: disable=unused-argument
        control: TrainerControl,
        **kwargs,  # pylint: disable=unused-argument
    ):
        if is_main_process():
            try:
                artifact = wandb.Artifact(name="axolotl-config", type="config")
                artifact.add_file(local_path=self.axolotl_config_path)
                wandb.run.log_artifact(artifact)
                LOG.info("Axolotl config has been saved to WandB as an artifact.")
            except (FileNotFoundError, ConnectionError) as err:
                LOG.warning(f"Error while saving Axolotl config to WandB: {err}")
        return control
