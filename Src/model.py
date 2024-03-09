from ast import arg
import torch
from transformer_lens import HookedTransformer
from transformers import AutoModelForCausalLM, AutoTokenizer
from .utils import  get_predictions
from abc import abstractmethod
torch.set_grad_enabled(False)


class ModelConfig:
    def __init__(self, **config):
        self.model_name:str
        self.n_layers:int
        self.n_heads:int
        self.full_config:object
        for key, value in config.items():
            setattr(self, key, value)

    def update_config(self, **config):
        for key, value in config.items():
            setattr(self, key, value)

    def get_config(self, key):
        return getattr(self, key, None)

class BaseModel:
    def __init__(self, model_name:str, *args, **kwargs):
        self.model = None
        self.cfg = ModelConfig(model_name = model_name)
        if model_name in ['Llama-2-7b-hf']:
            self.predict_with_space = False
        elif model_name in ['gpt2', 'gpt2-medium', 'gpt2-large', 'gpt2-xl', "pythia-6.9b"]:
            self.predict_with_space = True
        else:
            raise NotImplementedError(f"Model {model_name} does not have a predict_with_space attribute: pleas check the model behavior and add it manually")
        self.initialize_model(model_name, *args, **kwargs)
        
    def __call__(self, *args, **kwargs):
        if self.model is None:
            raise NotImplementedError("Model has not been initialized")
        return self.model(*args, **kwargs)
        
        
    def initialize_model(self,model_name:str, *args, **kwargs):
        raise NotImplementedError("initialize_model method must be implemented")
    
    @abstractmethod
    def predict(self, prompt:str, k:int = 1, return_type:str = "logits"):
        pass
    
    @abstractmethod
    def tokenize(self, text:str, prepend_bos:bool = False) -> torch.Tensor:
        pass
    
    @abstractmethod
    def to_string_token(self, token):
        pass
    
    @classmethod
    def cuda(cls):
        return cls.cuda()
    
    def eval(self):
        if self.model is None:
            raise NotImplementedError("Model has not been initialized")
        self.model.eval()
            
    @abstractmethod
    def run_with_cache(self, *args, **kwargs):
        raise NotImplementedError("run_with_cache method must be implemented")
    
    @abstractmethod
    def unembed(self):
        raise NotImplementedError("unembed method must be implemented")

class WrapHookedTransformer(BaseModel):
    


    def initialize_model(self, model_name:str, *args, **kwargs):
        self.model = HookedTransformer.from_pretrained(model_name, *args, **kwargs)
        self.device = self.model.cfg.device
        self.tokenizer = self.model.tokenizer

        self.cfg.update_config(
            n_layers =  self.model.cfg.n_layers, 
            n_heads = self.model.cfg.n_heads,
            full_config = self.model.cfg)    
        
    def predict(self, prompt: str, k: int = 1, return_type: str = "logits"):
        logits = self.model(prompt, return_type="logits")
        return get_predictions(self, logits, k, return_type)
    
    def tokenize(self, text:str, prepend_bos:bool = False):
        return self.model.to_tokens(text, prepend_bos)
    
    def to_string_token(self, token):
        return self.to_string_token(token)
    
    def run_with_cache(self, *args, **kwargs):
        return self.model.run_with_cache(*args, **kwargs)
    
    def unembed(self):
        return self.model.W_U
    
    
class WrapAutoModelForCausalLM(BaseModel):
    def initialize_model(self, model_name: str, device:str, *args, **kwargs):
        self.model = AutoModelForCausalLM.from_pretrained(model_name, *args, **kwargs)
        self.tokenizer = AutoTokenizer.from_pretrained(model_name)
        self.device = self.model.device
        if device == "cuda":
            self.model = self.model.cuda()
        #print warnings for the configuration

        self.cfg.update_config(
            n_layers =  self.model.config.n_layer, 
            n_heads = self.model.config.n_head,
            full_config = self.model.config)                               
        
    def tokenize(self, text: str, prepend_bos: bool = False):
        tokens = self.tokenizer.encode(text, return_tensors="pt", add_special_tokens=True)
        return tokens
    
    def to_string_token(self, tokens:torch.Tensor):
        #for each token in the tensor, convert it to string 
        assert tokens.shape[0] == 1, "Batch size must be 1"
        assert len(tokens.shape) == 2, "Token must be a 2D tensor"
        list_tokens = tokens.squeeze().tolist()
        string_tokens = []
        for t in list_tokens:
            string_tokens.append(self.tokenizer.decode(t))
        return string_tokens

    
class ModelFactory():
    @staticmethod
    def create(model_name:str, hf_model:bool=False):
        if hf_model:
            return WrapAutoModelForCausalLM(model_name)
        else:
            return WrapHookedTransformer(model_name)