from abc import abstractmethod
from cgitb import Hook
import re

import torch
from torch._tensor import Tensor
from torch.utils.data import Dataset
import json
from tqdm import tqdm
import random
from typing import Optional, Tuple, Union
from src.model import WrapHookedTransformer
from transformers import AutoModelForCausalLM
from transformer_lens import HookedTransformer
import logging
import os


class BaseDataset(Dataset):
    def __init__(
        self,
        path: str,
        slice: Optional[int] = None,
        premise: str = "Redefine:",
        similarity: Tuple[bool, int, str] = (False, 0, "input"),
    ):
        self.full_data = json.load(open(path))
        if slice is not None:
            self.full_data = self.full_data[:slice]
        self.premise = premise
        self.similarity = similarity

        self.lengths = self._get_lenghts_and_tokenize()

        self.prompts = []
        self.tokenized_prompts = []
        self.targets = []
        self.obj_pos = []

    def __len__(self):
        if len(self.prompts) == 0:
            raise ValueError("Dataset is empty: please call set_len() first")
        return len(self.prompts)

    def __getitem__(self, idx):
        if len(self.prompts) == 0:
            raise ValueError("Dataset is empty: please call set_len() first")
        return {
            "prompt": self.prompts[idx],
            "input_ids": self.tokenized_prompts[idx],
            "target": self.targets[idx],
            "obj_pos": self.obj_pos[idx],
        }

    def get_lengths(self):
        return self.lengths
    
    def slice_to_fit_batch(self, batch_size: int):
        max_data_size = (len(self.data) // batch_size) * batch_size
        self.slice(max_data_size)
        
    def slice(self, slice: int):
        raise NotImplementedError

    def _get_lenghts_and_tokenize(self):
        """
        Tokenize the prompts and apply the similarity if needed along with the premise.
        Return the lenghts of the dataset
        """
        lenghts = []
        for d in self.full_data:
            prompt = d["template"].format(self.premise, d["target_new"])
            d["prompt"] = prompt
            d["tokenized_prompt"] = self._tokenize_prompt(prompt, True)  # ( L)
            target_new_token = self._tokenize_target(d["target_new"], False).cuda()  # (1)
            d["target_new_token"] = target_new_token
            target_true_token = self._tokenize_target(d["target_true"], False).cuda()  # (1)
            d["target_true_token"] = target_true_token
            d["targets"] = torch.cat(
                [target_true_token, target_new_token], dim=0
            )  # (2)
            obj_pos_indices = (d["tokenized_prompt"] == target_new_token).nonzero(as_tuple=True)[0]
            if obj_pos_indices.size(0) > 0:
                d["obj_pos"] = obj_pos_indices[0].item()
            else:
                raise ValueError("Target not found in prompt")
            d["length"] = d["tokenized_prompt"].shape[0]
            if d["length"] not in lenghts:
                lenghts.append(d["length"])
                
        if self.similarity[0] is True:
            self.apply_similarity() #TODO test this and make sure it works 
            
        self._clear_cache()     # free up memory, we don't need the model anymore
        
        return lenghts
    
    def _clear_cache(self):
        """
        Remove model and tokenizer from memory to free up space
        """
        self.model = None
        self.tokenizer = None
        torch.cuda.empty_cache()

    def cuda(self):
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        if device == "cpu":
            logging.warning("Warning: CUDA not available, using CPU")
        self.targets = [t.to(device) for t in self.targets]
        self.tokenized_prompts = [t.to(device) for t in self.tokenized_prompts]
        self.obj_pos = [t.to(device) for t in self.obj_pos]
        
    def get_len(self):
        return self.len

    def set_len(self, length: int):
        """
        Set the length of the dataset to the given length, and filter the data accordingly
        """
        self.len = length
        self.prompts = []
        self.tokenized_prompts = []
        self.targets = []
        self.obj_pos = []
        self.data = []

        self.data = [d for d in self.full_data if d["length"] == length]
        # filter data by length
        self.prompts = [d["prompt"] for d in self.data]
        self.tokenized_prompts = [ d["tokenized_prompt"] for d in self.data]
        self.targets = [d["targets"] for d in self.data]
        self.obj_pos = [d["obj_pos"] for d in self.data]
        self.original_index = [
            i for i, d in enumerate(self.full_data) if d["length"] == length
        ]

        # if self.similarity[0] is True:
        #     self.apply_similarity()

        assert self.check_duplicate()

    def apply_similarity(self):
        """
        Apply the similarity to the dataset
        """
        # similarity level
        similarity_level = self.similarity[1]
        similarity_type = self.similarity[2]

        for d in tqdm(self.full_data, desc="Applying similarity", total=len(self.full_data)):
            similar_token_str, similar_token = self.get_similar_token(
                d, similarity_level, similarity_type
            )
            d["prompt"] = d["prompt"].replace(d["target_new"], similar_token_str)
            d["tokenized_prompt"][d["obj_pos"]] = similar_token[0, 0]
            d["targets"][1] = similar_token[0, 0]
            d["target_new"] = similar_token_str
            d["target_new_token"] = similar_token
            

        # for idx in range(self.__len__()):
        #     similar_token_str, similar_token = self.get_similar_token(
        #         idx, similarity_level, similarity_type
        #     )
        #     self.prompts[idx] = self.prompts[idx].replace(self.data[idx]["target_new"], similar_token_str)
        #     self.tokenized_prompts[idx][0, self.data[idx]["obj_pos"]] = similar_token[0, 0]
        #     self.targets[idx][0, 1] = similar_token[0, 0]
        #     self.data[idx]["target_new"] = similar_token_str
        #     self.data[idx]["target_new_token"] = similar_token
            

    def check_duplicate(self):
        seen = set()
        for i, d in enumerate(self.full_data):
            if d["prompt"] in seen:
                for j, d2 in enumerate(self.full_data):
                    if j == i:
                        continue
                    if d["prompt"] == d2["prompt"]:
                        if d["target_new"] == d2["target_new"]:
                            if d["target_true"] == d2["target_true"]:
                                return False
            seen.add(d["prompt"])
        return True


    def get_similar_token(self, data_point: dict, similarity_level: int, similarity_type: str) -> Tuple[str, torch.Tensor]:
        token_to_be_similar_str = data_point["target_true"]
        token_to_be_similar = data_point["target_true_token"]
        base_prompt = data_point["base_prompt"]
        return self._get_similar_token(token_to_be_similar_str, token_to_be_similar, similarity_level, similarity_type, base_prompt)

    @abstractmethod
    def _tokenize_prompt(self, prompt: str, prepend_bos: bool) -> torch.Tensor:
        pass
    
    @abstractmethod
    def _tokenize_target(self, target:str, prepend_bos:bool) -> torch.Tensor:
        pass
    
    @abstractmethod
    def _get_similar_token(self, token_to_be_similar_str: str, token_to_be_similar: torch.Tensor, similarity_level: int, similarity_type: str, base_prompt:str) -> Tuple[str, torch.Tensor]:
        pass
    
    
class TlensDataset(BaseDataset):
    def __init__(
        self,
        path: str,
        model: Union[WrapHookedTransformer, str, HookedTransformer],
        slice: Optional[int] = None,
        premise: str = "Redefine:",
        similarity: Tuple[bool, int, str] = (False, 0, "input"),
    ):
        if isinstance(model, str):
            self.model = WrapHookedTransformer.from_pretrained(model)
        else:
            self.model = model
        self.model.eval()
        super().__init__(path, slice, premise, similarity)
        
    def _tokenize_prompt(self, prompt: str, prepend_bos: bool) -> torch.Tensor:
        tokens = self.model.to_tokens(prompt, prepend_bos).squeeze(0)
        assert len(tokens.shape) == 1
        return tokens
    
    def _tokenize_target(self, target: str, prepend_bos: bool) -> Tensor:
        if self.model.predict_with_space is False:
            #remove the first space
            target = target[1:]
        tokens = torch.tensor([self.model.to_tokens(target, prepend_bos).squeeze(0)[0]])
        print(self.model.to_str_tokens(target,prepend_bos))
        assert tokens.shape[0] == 1, "tokens is not a 1D tensor with one element (the target)"
        return tokens
        
    def _get_similar_token(self, token_to_be_similar_str: str, token_to_be_similar: torch.Tensor, similarity_level: int, similarity_type: str, base_prompt:str) -> Tuple[str, torch.Tensor]:
        if similarity_type == "input":
            with torch.no_grad():
                token_embedding = self.model.W_E[token_to_be_similar].squeeze(0)
                embeddings = self.model.W_E
            
            cosine_similarity = torch.nn.functional.cosine_similarity(embeddings, token_embedding, dim=-1)
            cosine_similarity, sorted_indices = cosine_similarity.sort(descending=True)
            
            sorted_indices = sorted_indices[1:]
            cosine_similarity = cosine_similarity[1:]
            
            group = {}
            group[1] = sorted_indices[cosine_similarity < torch.quantile(cosine_similarity, 0.25)]
            group[2]= sorted_indices[(cosine_similarity >= torch.quantile(cosine_similarity, 0.25)) & (cosine_similarity < torch.quantile(cosine_similarity, 0.5))]
            group[3] = sorted_indices[(cosine_similarity >= torch.quantile(cosine_similarity, 0.5)) & (cosine_similarity < torch.quantile(cosine_similarity, 0.75))]
            group[4] = sorted_indices[cosine_similarity >= torch.quantile(cosine_similarity, 0.75)]
            
            sampled_token_idx = torch.randint(0, len(group[similarity_level]), (1,)).item()
            sampled_token = group[similarity_level][sampled_token_idx]
            sampled_token_str = self.model.to_string(sampled_token.item())
            assert sampled_token_str != token_to_be_similar_str, "sampled_token_str is the same as token_to_be_similar_str"
            assert sampled_token.shape[0] == 1, "sampled_token is not a 1D tensor"
            assert len(sampled_token.shape) == 2, "sampled_token is not a 2D tensor"
            assert isinstance(sampled_token_str, str), "sampled_token_str is not a string"
            return sampled_token_str, sampled_token.unsqueeze(0).unsqueeze(0)
        elif similarity_type == "output":
            raise NotImplementedError
        else:
            raise ValueError("similarity_type must be either 'input' or 'output'")

class HFDataset(BaseDataset):
    def __init__(
        self,
        model: Union[AutoModelForCausalLM, str],
        tokenizer,
        path: str,
        slice: Optional[int] = None,
        premise: str = "Redefine:",
        similarity: Tuple[bool, int, str] = (False, 0, "input"),
    ):
        if isinstance(model, str):
            self.model = AutoModelForCausalLM.from_pretrained(model)
        else:
            self.model = model
        self.tokenizer = tokenizer
        super().__init__(path, slice, premise, similarity)
        
    def _tokenize_prompt(self, prompt: str, prepend_bos: bool) -> torch.Tensor:
        tokens = self.tokenizer.encode(prompt, return_tensors="pt", add_special_tokens=True).squeeze(0)
        assert len(tokens.shape) == 1
        return tokens
    
    def _tokenize_target(self, target: str, prepend_bos: bool) -> torch.Tensor:
        tokens = self.tokenizer.encode(target, return_tensors="pt", add_special_tokens=False).squeeze(0)
        assert len(tokens.shape) == 1
        return tokens
    
    def _get_similar_token(self, token_to_be_similar_str: str, token_to_be_similar: torch.Tensor, similarity_level: int, similarity_type: str, base_prompt:str) -> Tuple[str, torch.Tensor]:
        if similarity_type == "input":
            if token_to_be_similar.ndim == 1:
                token_to_be_similar = token_to_be_similar.cuda()
            elif token_to_be_similar.ndim > 1:
                token_to_be_similar = token_to_be_similar[0, 0].unsqueeze(0).cuda()
            else:
                token_to_be_similar = token_to_be_similar[0,0].unsqueeze(0).cuda()
        
            with torch.no_grad():
                embeddings = self.model.get_input_embeddings().cuda() # type: ignore
                token_to_be_similar_emb = embeddings(token_to_be_similar)
                
            cosine_similarity = torch.nn.functional.cosine_similarity(embeddings.weight, token_to_be_similar_emb.unsqueeze(0), dim=-1).squeeze()
                
            cosine_similarity, sorted_indices = cosine_similarity.sort(descending=True)
            
            sorted_indices = sorted_indices[1:]
            cosine_similarity = cosine_similarity[1:]
            print("Cosine similarity shape", cosine_similarity.shape)
            group = {}
            group[1] = sorted_indices[cosine_similarity < torch.quantile(cosine_similarity, 0.25)]
            group[2]= sorted_indices[(cosine_similarity >= torch.quantile(cosine_similarity, 0.25)) & (cosine_similarity < torch.quantile(cosine_similarity, 0.5))]
            group[3] = sorted_indices[(cosine_similarity >= torch.quantile(cosine_similarity, 0.5)) & (cosine_similarity < torch.quantile(cosine_similarity, 0.75))]
            group[4] = sorted_indices[cosine_similarity >= torch.quantile(cosine_similarity, 0.75)]
            
            sampled_token_idx = torch.randint(0, len(group[similarity_level]), (1,)).item()
            sampled_token = group[similarity_level][sampled_token_idx]
            sampled_token_str = self.tokenizer.decode(sampled_token.item())
            sampled_token = sampled_token.unsqueeze(0).unsqueeze(0)
            assert sampled_token_str != token_to_be_similar_str, "sampled_token_str is the same as token_to_be_similar_str"
            assert sampled_token.shape[0] == 1, "sampled_token is not a 1D tensor"
            assert len(sampled_token.shape) == 2, "sampled_token is not a 2D tensor"
            assert isinstance(sampled_token_str, str), "sampled_token_str is not a string"
            print("Original token:", token_to_be_similar_str, "Similar token:", sampled_token_str)
            return sampled_token_str, sampled_token
        
        elif similarity_type == "output":
            hidden_state = self.model(token_to_be_similar, output_hidden_states=True)["hidden_states"][-1] # type: ignore
            print("HIDDEN STATE SHAPE", hidden_state.shape)
            if hidden_state.ndim == 2:
                hidden_state = hidden_state[-1, :]
            if hidden_state.ndim == 3:
                hidden_state = hidden_state[-1, -1, :]
            
            #rotate the vector
            factors = [1, 0.5, 0.25, 0.0]  # Adjust these values as needed

            # Sampling new vectors
            similar_vectors = []
            similarity_values = []
            for factor in factors:
                random_vector = torch.randn_like(hidden_state)
                random_vector = random_vector / random_vector.norm()
                hidden_state = hidden_state / hidden_state.norm()

                # Interpolate between the original and a random vector
                new_vector = factor * hidden_state + (1 - factor) * random_vector
                new_vector = new_vector / new_vector.norm()  # Re-normalize the vector
                similarity_values.append(torch.nn.functional.cosine_similarity(hidden_state, new_vector, dim=-1))
                similar_vectors.append(new_vector)

            
            group = {}
            group[1] = similar_vectors[0]
            group[2] = similar_vectors[1]
            group[3] = similar_vectors[2]
            group[4] = similar_vectors[3]

            
            group_token = {}
            for i, similar_vector in enumerate(similar_vectors):
                with torch.no_grad():
                    logit = self.model.get_output_embeddings()(similar_vector) # type: ignore
                    # select the token with the highest logit
                    token = logit.argmax()
                    token_str = self.tokenizer.decode(token.item())
                    group_token[i+1] = (token_str, token)
            
            print("Original token:", token_to_be_similar_str)
            print("Similar tokens:", group_token[1][0], group_token[2][0], group_token[3][0], group_token[4][0])       
            print("Similarity values:", similarity_values[0].item(), similarity_values[1].item(), similarity_values[2].item(), similarity_values[3].item())     
            return group_token[similarity_level][0], group_token[similarity_level][1].unsqueeze(0).unsqueeze(0) 
        elif similarity_type == "output2":
            raise NotImplementedError
            hidden_state = self.model(token_to_be_similar, output_hidden_states=True)["hidden_states"][-1] # type: ignore
            print("HIDDEN STATE SHAPE", hidden_state.shape)
            if hidden_state.ndim == 2:
                hidden_state = hidden_state[-1, :]
            if hidden_state.ndim == 3:
                hidden_state = hidden_state[-1, -1, :]
                
            vocab = self.tokenizer.get_vocab()
            similar_tokens = []
            
            for word, idx in vocab.items():
                with torch.no_grad():
                    word_embedding = self.model(self.tokenizer.encode(word, return_tensors="pt", add_special_tokens=False).to(self.model.device), output_hidden_states=True)["hidden_states"][-1][-1,:]
                    cosine_similarity = torch.nn.functional.cosine_similarity(hidden_state, word_embedding, dim=-1)
                    print("word", word, "cosine_similarity", cosine_similarity)
            return "test", torch.tensor([[0]])
        else:
            raise ValueError("similarity_type must be either 'input' or 'output'")


class SampleDataset:
    def __init__(self, path:str, model, save_path:str, tokenizer:Optional[object]):
        self.data = json.load(open(path))
        self.model = model
        self.save_path = save_path
        self.checkpoint_size = 500
        if type(model) == WrapHookedTransformer:
            self.model_type = "WrapHookedTransformer"
            self.tokenizer = model.tokenizer  
        else:
            self.model_type = "AutoModelForCausalLM"
            try:
                self.tokenizer = tokenizer
            except AttributeError:  
                raise ValueError("With HuggingFace models, you must pass a tokenizer")

    def sample(self, size:int=10000):
        if type(self.model) == WrapHookedTransformer:
            self.sample_dataset_tlens(size)
        else:
            self.sample_dataset_hf(size)
    
    def sample_dataset_tlens(self, size:int):
        random.seed(42)
        new_data, index = self.load_from_checkpoint()
        
        random.shuffle(self.data)
        self.data = self.data[index:]
        size = size - len(new_data)
        new_data = []
        random.shuffle(self.data)
        with tqdm(total=size) as pbar:
            for i,d in enumerate(self.data):
                if i % self.checkpoint_size == 0:
                    self.checkpoint(i, new_data)
                # empty_prompt = d["template"].format("Redefine", self.model.tokenizer.pad_token)
                empty_prompt = d["base_prompt"]
                if self.model.predict(empty_prompt)[1][0] == d["target_true"]:
                    new_data.append(d)
                    if len(new_data) == size:
                        break
                pbar.update(len(new_data)-pbar.n)
            self.data = new_data
            
    def sample_dataset_hf(self, size:int):
        random.seed(42)
        new_data, index = self.load_from_checkpoint()
        
        random.shuffle(self.data)
        self.data = self.data[index:]
        print(len(self.data), index)
        
        with tqdm(total=size) as pbar:
            pbar.update(len(new_data))
            for i,d in enumerate(self.data):
                if i % self.checkpoint_size == 0:
                    self.checkpoint(i, new_data)
                empty_prompt = d["base_prompt"]
                #encode the prompt
                input_ids = self.tokenizer.encode(empty_prompt, return_tensors="pt") #type: ignore
                input_ids = input_ids.to(self.model.device) #type: ignore
                target_true = self.tokenizer.encode(d["target_true"], return_tensors="pt", add_special_tokens=False) #type: ignore
                #predict the next token
                logits = self.model(input_ids)["logits"][0, -1, :].cpu()
                #get the index of the predicted token
                index = logits.argmax()
                # check if the predicted token is the target

                if index in target_true:
                    new_data.append(d)
                    if len(new_data) == size:
                        break
                pbar.update(len(new_data)-pbar.n)
            self.data = new_data
    
    def save(self):
        json.dump(self.data, open(self.save_path, "w"), indent=2)
    
    def checkpoint(self, size:int, data:list):
        #create checkpoint folder

        if not os.path.isdir("../data/checkpoints"):
            os.mkdir("../data/checkpoints")
        #save the data
        #split save path 
        save_path = self.save_path.split("/")[2]
        json.dump(data, open(f"../data/checkpoints/{save_path}", "w"), indent=2)
        
    def load_from_checkpoint(self):
        save_path = self.save_path.split("/")[2]
        # check if the checkpoint exists
        if not os.path.isfile(f"../data/checkpoints/{save_path}"):
            return [], 0
        print("Starting from checkpoint")
        data = json.load(open(f"../data/checkpoints/{save_path}"))
        # get index of the last data point
        index = len(data) - 1
        if index < 0:
            return [], 0
        return data, index
    
    
class DatasetGenerator():
    def __init__(self, path):
        self.data = json.load(open(path))
    
    def generate_dataset(self, model, lenghts=[17,19,23]):
    
        my_data = []
        for i,d in tqdm(enumerate(self.data), total=len(self.data), desc="Generating dataset"):
            target_new = " " + d["requested_rewrite"]["target_true"]["str"]
            target_true = " " + d["requested_rewrite"]["target_new"]["str"]
            if i % 50 == 0:
                unique_strs = set(json.dumps(d) for d in my_data)
                my_data = [json.loads(s) for s in unique_strs]
                print(len(my_data))
                # if len(my_data) > 1000:
                #     break
            for p in d["attribute_prompts"]:
                template = "Redefine: " + p + "{}" + ". " + p
                #find position of {} in template
                if len(model.to_str_tokens(template.format(model.tokenizer.pad_token))) not in lenghts:
                    continue
                try:
                    obj_pos = model.to_str_tokens(template.format(model.tokenizer.pad_token)).index(".") - 1
                except:  # noqa: E722
                    continue
                if target_true in template:
                    continue
                prediction = model.predict(template.format(model.tokenizer.pad_token))[1][0]
                copy_prediction = model.predict(template.format(target_new))[1][0]
                if prediction == target_true and copy_prediction == target_new:
                    my_data.append({
                        "prompt": p,
                        "template": template,
                        "prediction": prediction,
                        "copy_prediction": copy_prediction,
                        "target_true": target_true,
                        "target_new": target_new,
                        "length": len(model.to_str_tokens(template.format(model.tokenizer.pad_token))),
                        "lenght_copy": len(model.to_str_tokens(template.format(target_new))),
                        "obj_pos": obj_pos,
                    
                    })
            for p in d["neighborhood_prompts"]:
                template = "Redefine: " + p + "{}" + ". " + p
                #find position of {} in template
                if len(model.to_str_tokens(template.format(model.tokenizer.pad_token))) not in lenghts:
                    continue
                try:
                    obj_pos = model.to_str_tokens(template.format(model.tokenizer.pad_token)).index(".") - 1
                except: # noqa: E722
                    continue
                if target_true in template:
                    continue
                prediction = model.predict(template.format(model.tokenizer.pad_token))[1][0]
                copy_prediction = model.predict(template.format(target_new))[1][0]
                if prediction == target_true and copy_prediction == target_new:
                    # check if is a duplicate
                    
                    my_data.append({
                        "prompt": p,
                        "template": template,
                        "prediction": prediction,
                        "copy_prediction": copy_prediction,
                        "target_true": target_new,
                        "target_new": target_true,
                        "length": len(model.to_str_tokens(template.format(model.tokenizer.pad_token))),
                        "lenght_copy": len(model.to_str_tokens(template.format(target_new))),
                        "obj_pos": obj_pos,
                    })
                    
        print("Number of examples:", len(my_data), "Number of possible lengths:", lenghts)
        self.my_data = my_data
        
    def save(self, path):
        json.dump(self.my_data, open(path, "w"), indent=2)
