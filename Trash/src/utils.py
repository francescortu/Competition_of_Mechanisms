from math import e
from re import sub
from numpy import object_
import torch
import warnings
from rich.panel import Panel
from rich.columns import Columns
from rich.text import Text
from rich.table import Table
import time
import os
import torch.nn.functional as F
from transformers import AutoModelForCausalLM, AutoTokenizer, GPTNeoXForCausalLM
from typing import Literal, Union

def check_dataset_and_sample(dataset_path, model_name, hf_model_name):
    if os.path.exists(dataset_path):
        print("Dataset found!")
        return
    else:
        

        print("Dataset not found, creating it:")
        model = AutoModelForCausalLM.from_pretrained(hf_model_name, device_map="auto", torch_dtype="auto")
        tokenizer = AutoTokenizer.from_pretrained(hf_model_name)
        model.eval()
        model = model.to("cuda")
        from src.dataset import SampleDataset

        sampler = SampleDataset(
            "../data/full_data.json",
            model=model,
            save_path=dataset_path,
            tokenizer=tokenizer,
        )
        sampler.sample()
        sampler.save()
        del model
        del sampler
        torch.cuda.empty_cache()
        return


def display_experiments(experiments, status):
    table = Table(show_header=True, header_style="bold magenta", expand=True)
    table.add_column("Experiment", style="dim", width=3)
    table.add_column("Status", width=1)
    table.add_column("Live Output")  # New column for future live output
    for experiment, stat in zip(experiments, status):
        table.add_row(
            experiment.__name__, stat, ""
        )  # Empty string for future live output
    return table


def display_config(config):
    config_items = [
        Text.assemble(("Model Name: ", "bold"), str(config.model_name)),
        Text.assemble(("Batch Size: ", "bold"), str(config.batch_size)),
        Text.assemble(("Dataset Path: ", "bold"), config.dataset_path),
        Text.assemble(("Dataset Slice: ", "bold"), str(config.dataset_slice)),
        Text.assemble(("Produce Plots: ", "bold"), str(config.produce_plots)),
        Text.assemble(("Normalize Logit: ", "bold"), str(config.normalize_logit)),
        Text.assemble(("Std Dev: ", "bold"), str(config.std_dev)),
        Text.assemble(("Total Effect: ", "bold"), str(config.total_effect)),
        Text.assemble(("Up-to-layer: ", "bold"), str(config.up_to_layer)),
        Text.assemble(("Experiment Name: ", "bold"), str(config.mech_fold)),
    ]

    columns = Columns(config_items, equal=True, expand=True)
    panel = Panel(columns, title="Configuration", border_style="green")
    return panel


def update_status(i, status):
    try:
        dots = "."
        while status[i] == "Running" or status[i].startswith("Running."):
            status[i] = "Running" + dots + " " * (3 - len(dots))  # Pad with spaces
            dots = dots + "." if len(dots) < 3 else "."
            time.sleep(0.5)
    except Exception as e:
        raise e


def update_live(live, experiments, status):
    while True:
        live.update(display_experiments(experiments, status))
        time.sleep(0.1)


def get_predictions(model, logits, k, return_type):
    if return_type == "probabilities":
        logits = torch.softmax(logits, dim=-1)
    if return_type == "logprob":
        logits = torch.log_softmax(logits, dim=-1)

    prediction_tkn_ids = logits[0, -1, :].topk(k).indices.cpu().detach().numpy()
    prediction_tkns = [model.to_string(tkn_id) for tkn_id in prediction_tkn_ids]
    best_logits = logits[0, -1, prediction_tkn_ids]

    return best_logits, prediction_tkns


def squeeze_last_dims(tensor):
    if len(tensor.shape) == 3 and tensor.shape[1] == 1 and tensor.shape[2] == 1:
        return tensor.squeeze(-1).squeeze(-1)
    if len(tensor.shape) == 2 and tensor.shape[1] == 1:
        return tensor.squeeze(-1)
    else:
        return tensor


def suppress_warnings(fn):
    def wrapper(*args, **kwargs):
        # Save the current warnings state
        current_filters = warnings.filters[:]
        warnings.filterwarnings("ignore")
        try:
            return fn(*args, **kwargs)
        finally:
            # Restore the warnings state
            warnings.filters = current_filters

    return wrapper


def embs_to_tokens_ids(noisy_embs, model):
    input_embedding_norm = F.normalize(noisy_embs, p=2, dim=2)
    embedding_matrix_norm = F.normalize(model.W_E, p=2, dim=1)
    similarity = torch.matmul(input_embedding_norm, embedding_matrix_norm.T)
    corrupted_tokens = torch.argmax(similarity, dim=2)
    return corrupted_tokens


def list_of_dicts_to_dict_of_lists(list_of_dicts):
    # Initialize an empty dictionary to store the result
    dict_of_lists = {}

    # Iterate over each dictionary in the list
    for d in list_of_dicts:
        # Iterate over each key-value pair in the dictionary
        for key, value in d.items():
            # If the key is not already in the result dictionary, add it with an empty list as its value
            if key not in dict_of_lists:
                dict_of_lists[key] = []
            # Append the value to the list corresponding to the key in the result dictionary
            dict_of_lists[key].append(value)

    return dict_of_lists


def dict_of_lists_to_dict_of_tensors(dict_of_lists):
    dict_of_tensors = {}
    for key, tensor_list in dict_of_lists.items():
        dict_of_tensors[key] = torch.stack(tensor_list)
    return dict_of_tensors

def get_aggregator(experiment: Literal["copyVSfact", "contextVSfact", "copyVSfact_factual"]):
    if "copyVSfact" in experiment:
        return aggregate_result_copyVSfact
    if experiment == "contextVSfact":
        return aggregate_result_contextVSfact

def aggregate_result_copyVSfact(
    pattern: torch.Tensor, object_positions: Union[torch.Tensor, int], length: int
) -> torch.Tensor:
    subject_1_1 = 5
    subject_1_2 = 6 if length > 15 else 5
    subject_1_3 = 7 if length > 17 else subject_1_2
    subject_2_1 = object_positions + 2
    subject_2_2 = object_positions + 3 if length > 14 else subject_2_1
    subject_2_3 = object_positions + 4 if length > 16 else subject_2_2
    subject_2_2 = subject_2_2 if subject_2_2 < length else subject_2_1
    subject_2_3 = subject_2_3 if subject_2_3 < length else subject_2_2
    last_position = length - 1
    object_positions_pre = object_positions - 1
    object_positions_next = object_positions + 1
    *leading_dims, pen_len, last_len = pattern.shape

    intermediate_aggregate = torch.zeros((*leading_dims, pen_len, 13))
    # aggregate for pre-last dimension
    intermediate_aggregate[..., 0] = pattern[..., :subject_1_1].mean(dim=-1)
    intermediate_aggregate[..., 1] = pattern[..., subject_1_1]
    intermediate_aggregate[..., 2] = pattern[..., subject_1_2]
    intermediate_aggregate[..., 3] = pattern[..., subject_1_3]
    if object_positions_pre > subject_1_3 + 1:
        intermediate_aggregate[..., 4] = pattern[
            ..., subject_1_3 + 1 : object_positions_pre
        ].mean(dim=-1)
    else:
        intermediate_aggregate[..., 4] = pattern[
            ..., subject_1_3 + 1
        ]
    intermediate_aggregate[..., 5] = pattern[..., object_positions_pre]
    intermediate_aggregate[..., 6] = pattern[..., object_positions]
    intermediate_aggregate[..., 7] = pattern[..., object_positions_next]
    intermediate_aggregate[..., 8] = pattern[..., subject_2_1]
    intermediate_aggregate[..., 9] = pattern[..., subject_2_2]
    intermediate_aggregate[..., 10] = pattern[..., subject_2_3]
    if last_position > subject_2_3 + 1:
        intermediate_aggregate[..., 11] = pattern[
            ..., subject_2_3 + 1 : last_position
        ].mean(dim=-1)
    else:
        intermediate_aggregate[..., 11] = pattern[
            ..., subject_2_3 
        ]
    intermediate_aggregate[..., 12] = pattern[..., last_position]
    return intermediate_aggregate

def aggregate_result_contextVSfact(
    pattern: torch.Tensor, object_positions: Union[torch.Tensor, int], length: int, subj_positions: int, batch_dim: int
) -> torch.Tensor:
    batch_size = pattern.shape[batch_dim]
    len_aggregate = 9

    if batch_dim == 0:
        pattern = pattern.transpose(0, 1)
    aggregate_result = torch.zeros((pattern.shape[0], batch_size, len_aggregate))

    for i in range(batch_size):
        single_subject_positions = subj_positions[i] # type: ignore
        aggregate_result[:, i, :] = aggregate_single_result_contextVSfact(pattern[:, i], object_positions[i], length, single_subject_positions)
    if batch_dim == 0:
        aggregate_result = aggregate_result.transpose(0, 1)
    
    
    return aggregate_result
    
def aggregate_single_result_contextVSfact(
    pattern: torch.Tensor, object_positions: int, length: int, subj_position: int
) -> torch.Tensor:
    subject_1 = subj_position
    subject_2 = subj_position + 1 if (length - subj_position) > 15 else subject_1
    subject_3 = subj_position + 2 if (length - subj_position) > 17 else subject_2
    object_positions_next = object_positions + 1 if object_positions < length - 1 else object_positions
    subject_pos_pre = subj_position - 1 if subj_position > 0 else 0
    last_position = length - 1
    
    
    *leading_dims, pen_len, last_len = pattern.shape
    if object_positions > 0:
        
        assert object_positions < subject_1, f"object_positions {object_positions} < subject_1 {subject_1}"
        
        
        intermediate_aggregate = torch.zeros((*leading_dims, pen_len, 9))   
        intermediate_aggregate[..., 0] = pattern[..., :object_positions].mean(dim=-1) # before object
        intermediate_aggregate[..., 1] = pattern[..., object_positions]
        if object_positions_next == subject_1:
            intermediate_aggregate[..., 2] = 0
        elif object_positions_next + 1 == subject_1:
            intermediate_aggregate[..., 2] = pattern[..., object_positions_next]
        else:
            assert object_positions_next < subject_1, "object_positions_next < subject_1"
            mypattern = pattern[..., object_positions_next:subject_pos_pre]
            mypattern[torch.isnan(mypattern)] = 0
            assert not torch.isnan(mypattern.mean(dim=-1)).any(), "nan in mypattern"
            intermediate_aggregate[..., 2] = mypattern.mean(dim=-1) # between object and subject
        intermediate_aggregate[..., 3] = pattern[..., subject_pos_pre]
        intermediate_aggregate[..., 4] = pattern[..., subject_1]
        intermediate_aggregate[..., 5] = pattern[..., subject_2]
        intermediate_aggregate[..., 6] = pattern[..., subject_3]
        if subject_3 + 1 == last_position:
            intermediate_aggregate[..., 7] = 0
        else:
            intermediate_aggregate[..., 7] = pattern[..., subject_3 + 1 : last_position].mean(dim=-1) # between subject and last
        intermediate_aggregate[..., 8] = pattern[..., last_position]
        
        
    # else:
    #     intermediate_aggregate = torch.zeros((*leading_dims, pen_len, 8))
    #     intermediate_aggregate[..., 0] = pattern[..., object_positions] 
    #     intermediate_aggregate[..., 1] = pattern[..., object_positions_next]
    #     intermediate_aggregate[..., 2] = pattern[..., object_positions_next + 1 : subject_1].mean(dim=-1) # between object and subject
    #     intermediate_aggregate[..., 3] = pattern[..., subject_1]    
    #     intermediate_aggregate[..., 4] = pattern[..., subject_2]
    #     intermediate_aggregate[..., 5] = pattern[..., subject_3]
    #     intermediate_aggregate[..., 6] = pattern[..., subject_3 + 1 : last_position].mean(dim=-1)
    #     intermediate_aggregate[..., 7] = pattern[..., last_position]
    return intermediate_aggregate
    
        
    
    
    
    
    
    raise NotImplementedError("Not implemented yet")