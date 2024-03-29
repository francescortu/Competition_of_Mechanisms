# Competition_of_Mechanisms


## Run the Experiments

### LogitLens, Logit Attribution, Attention Pattern
#### Notebooks/experiments.ipynb
You can run the experiments running the `notebooks/experiments.ipynb` notebook. This notebook contains the code to run the experiments for the logit lens, logit attribution, and attention pattern.

#### Script/run_all.py
You can run the experiment running the following command:
```bash
cd Script
python run_all.py
```
with the following arguments:
- `--model-name`: the name of the model to run the experiments on. It can be `gpt2` or `EleuhterAI/pythia-6.9b`.
- `--batch N` : the batch size to use for the experiments. ( Suggested 40 for gpt2, 10 for pythia) 
- `--experiment copyVSfact` : the experiment to run.
- `--logit-attribution` : if you want to run the logit attribution experiment.
- `--logit-len` : if you want to run the logit lens (fig 2) experiment.
- `--pattern`: if you want to retrieve the attention pattern.
  
The script will create a folder in the `Results/copyVSfact` directory with the name of the model.

Example:
```bash
cd Script
python run_all.py --model-name gpt2 --batch 40 --experiment copyVSfact --logit-attribution 
```



### Attention Modification
To run the attention modification experiments, you should look at the `notebooks/attention_modification.ipynb` notebook. This notebook contains the code to run the experiments for the attention modification.

## Plot
You can plot using the `src_figure/PaperPlot_multiple_subject.Rmd`.

## TODO

- [X] 1. Update aggregator for attribution
- [X] 2. Update aggregator for head pattern
- [X] 3. Check the correctness of the batch size in the aggregator. If it is not correct, modify it to have a standard shape
    - [X] LogitLens
    - [X] Attribution
    - [X] HeadPattern
- [ ] Clean subject data for gpt2
- [ ] Clean subject data for pythia
- [X] Modify the plot for the new positions
- [ ] 4. Update the aggregator for the ablation
- [X] 5. Implement an easy-attention modification class for single group of heads
- [ ] 6. Add TypeDict class for the type hinting, check the correctess of all the code
- [ ] 7. Unify the aggregation of the positions