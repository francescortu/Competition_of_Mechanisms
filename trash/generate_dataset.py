import sys
sys.path.append('..')
sys.path.append('../src')
sys.path.append('../data')

import torch
from transformer_lens import HookedTransformer
import json
from src.model import WrapHookedTransformer
from tqdm import tqdm

import transformer_lens.utils as utils
from transformer_lens.utils import get_act_name
from functools import partial
from transformer_lens import patching

LOAD = True
MODEL_NAME = "gpt2"
SAVE_PATH = "dataset_{}_f.json".format(MODEL_NAME)




script_dir = os.path.dirname(os.path.abspath(__file__))
json_path = os.path.join(script_dir, '..', 'data', 'counterfact.json')
data = json.load(open(json_path))

model = WrapHookedTransformer.from_pretrained(MODEL_NAME, device="cuda")


dataset = []
for d in tqdm(data, total=len(data)):
    for i in range(len(d["attribute_prompts"])):
        dataset.append(
            {"prompt": d["attribute_prompts"][i],
             "target": " " + d["requested_rewrite"]["target_new"]["str"]}
        )
    
    for i in range(len(d["neighborhood_prompts"])):
        dataset.append(
            {"prompt": d["neighborhood_prompts"][i],
             "target": " " + d["requested_rewrite"]["target_true"]["str"]}
        )
print(len(dataset))

if LOAD:
    script_dir = os.path.dirname(os.path.abspath(__file__))
    json_path = os.path.join(script_dir, '..', 'data', 'dataset_per_length.json')
    dataset_per_length = json.load(open(json_path))

else:
    dataset_per_length = {}
    for d in tqdm(dataset, total=len(dataset)):
        orthogonal_token = model.to_orthogonal_tokens(d["target"])
        d["premise"] = d["prompt"] + orthogonal_token + " " + d["prompt"]
        d["orthogonal_token"] = orthogonal_token
        d["length"] = len(model.to_str_tokens(d["premise"]))
        if d["length"] not in dataset_per_length:
            dataset_per_length[d["length"]] = []
        dataset_per_length[d["length"]].append(d)
    
    
#create a pytorch dataloader for each length
dataloaders = {}
for length in sorted(dataset_per_length.keys()):
    dataloaders[length] = torch.utils.data.DataLoader(dataset_per_length[length], batch_size=100, shuffle=True)
    
    
def append_to_dataset(dataset, batch, i, target_probs, orthogonal_probs):
    dataset.append({
        "prompt": batch["prompt"][i],
        "target": batch["target"][i],
        "premise": batch["premise"][i],
        "orthogonal_token": batch["orthogonal_token"][i],
        "length": float(batch["length"][i].cpu().detach().numpy().item()),
        "target_probs": float(target_probs[i].cpu().detach().numpy().item()),
        "orthogonal_probs": float(orthogonal_probs[i].cpu().detach().numpy().item()),
    })

target_probs_mean = {}
orthogonal_probs_mean = {}
target_win = {}
orthogonal_win = {}
other_win = {}
target_win_over_orthogonal = {}
target_win_dataset = []
orthogonal_win_dataset = []
count=0
for length in sorted(dataset_per_length.keys()):
    target_probs_mean[length] = []
    orthogonal_probs_mean[length] = []
    target_win[length] = 0
    orthogonal_win[length] = 0
    other_win[length] = 0
    target_win_over_orthogonal[length] = 0

    for batch in tqdm(dataloaders[length]):
        logit = model(batch["premise"])
        probs = torch.softmax(logit, dim=-1)
        batch_index = torch.arange(probs.shape[0])
        
        target_tokens = model.to_tokens(batch["target"], prepend_bos=False).squeeze(-1)
        target_probs = probs[batch_index, -1, target_tokens]
        
        orthogonal_tokens = model.to_tokens(batch["orthogonal_token"], prepend_bos=False).squeeze(-1)
        if len(orthogonal_tokens.shape) == 2:
            orthogonal_tokens = orthogonal_tokens[:, 0]
        orthogonal_probs = probs[batch_index, -1, orthogonal_tokens]
        
        predictions = probs[:, -1, :].max(dim=-1)[0]
        clean_predictions = probs[:, -1, :].max(dim=-1)[1]

        raw_probs = torch.softmax(model(batch["prompt"]), dim=-1)
    

        for i in range(len(batch["premise"])):
            string_row_pred = model.to_string(raw_probs[i, -1, :].argmax(dim=-1))
            if string_row_pred != batch["target"][i]:
                # count+=1
                continue
            count+=1
            if target_probs[i] == predictions[i]:
                target_win[length] += 1
                append_to_dataset(target_win_dataset, batch, i, target_probs, orthogonal_probs)
            elif orthogonal_probs[i] == predictions[i]:
                orthogonal_win[length] += 1
                append_to_dataset(orthogonal_win_dataset, batch, i, target_probs, orthogonal_probs)
            if target_probs[i] > orthogonal_probs[i]:
                target_win_over_orthogonal[length] += 1

        target_probs_mean[length].append(target_probs.mean().item())
        orthogonal_probs_mean[length].append(orthogonal_probs.mean().item())
        print(count)
    
    target_probs_mean[length] = sum(target_probs_mean[length]) / len(target_probs_mean[length])
    orthogonal_probs_mean[length] = sum(orthogonal_probs_mean[length]) / len(orthogonal_probs_mean[length])


# #sum target win and orthogonal win and target_win_over_orthogonal for each length
target_win = sum(target_win.values())
orthogonal_win = sum(orthogonal_win.values())
target_win_over_orthogonal = sum(target_win_over_orthogonal.values())

n_samples = sum([len(dataset_per_length[length]) for length in dataset_per_length.keys()])
#print percentages over the total number of examples
print("target win", target_win / n_samples)
print("orthogonal win", orthogonal_win / n_samples)
print("target win over orthogonal", target_win_over_orthogonal / n_samples)

target_win_dataset = [d for d in target_win_dataset if d["target_probs"] < 0.2]
orthogonal_win_dataset = [d for d in orthogonal_win_dataset if d["orthogonal_probs"] < 0.2]
# Get the directory of the current script
script_dir = os.path.dirname(os.path.abspath(__file__))

# Construct the paths to the JSON files
dataset_path = os.path.join(script_dir, '..', 'data', SAVE_PATH)

dataset = {
    "memorizing_win": target_win_dataset,
    "copying_win": orthogonal_win_dataset,
}

# Dump the data to the JSON files
with open(dataset_path, 'w') as target_file:
    json.dump(dataset, target_file, indent=4)
