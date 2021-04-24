#!/usr/bin/env python

# Get command-line arguments
import argparse
arg_parser = argparse.ArgumentParser()
arg_parser.add_argument("train_file",
                help="A list of training data files.")
arg_parser.add_argument("valid_file",
                help="A list of validation data files.")
arg_parser.add_argument("-c", "--config", dest="config_file", required=True,
                help="A file storing configuration options.")
# ouput options
arg_parser.add_argument("--no_tensorboard", action="store_false", default=None, dest="tensorboard",
                help="Do not write any output to a tensorboard file.")
arg_parser.add_argument("--no_write", action="store_false", default=None, dest="write",
                help="Don't write anything to file and log everything to console.")
arg_parser.add_argument("--write_test_predictions", action="store_true", default=None,
                help="If set will write predictions over validation set to file after training is complete.")
arg_parser.add_argument("--output_path", help="Directory to place output.")
arg_parser.add_argument("--run_name", help="Name of this run to use for output files.")

# dataset options
arg_parser.add_argument("--balance", type=str, choices=['balanced', 'unmasked', 'all'], default=None,
                help="Decide which set of training labels to use.")
arg_parser.add_argument("--no_shuffle", action="store_false", dest="shuffle", default=None,
                help="Don't shuffle training data.")

# training options
arg_parser.add_argument("--checkpoint_every", type=int,
                help="How often to write a checkpoint file to disk. Default is once at end of training.")
arg_parser.add_argument("--eval_every", type=int,
                help="How often to compute metrics over the training and validation data.")
arg_parser.add_argument("--batch_size", type=int,
                help="Size of the mini-batches.")
arg_parser.add_argument("--epochs", type=int,
                help="Number of epochs to train for.")
arg_parser.add_argument("--weight_method", type=str, choices=['none', 'batch', 'dataset'], default=None,
                help="How to weight each class when training on dataset.")

# misc options
arg_parser.add_argument("--single_gpu", action="store_true", default=None,
                help="Don't distribute across multiple GPUs even if available, just use one.")
arg_parser.add_argument("--no_random", action="store_true", default=None,
                help="Use a fixed random seed (useful for debugging).")
arg_parser.add_argument("--debug", action="store_true", default=None,
                help="Print additonal debugging information.")

# Standard packages
import os
import json
import logging
import shutil
from datetime import datetime
from os.path import join as ospj
import pickle

# Third party modules
import numpy as np
import torch
from torch.utils.tensorboard import SummaryWriter
from torch_geometric.nn import DataParallel
from torch_geometric.data import DataLoader, DataListLoader
from torch_geometric.transforms import Compose, FaceToEdge, PointPairFeatures, GenerateMeshNormals, Cartesian, TwoHop

# Geobind modules
from geobind.nn.utils import loadDataset, classWeights
from geobind.nn import Trainer, Evaluator
from geobind.nn.models import NetConvPool, PointNetPP, MultiBranchNet, FFNet
from geobind.nn.metrics import reportMetrics
from geobind.nn.transforms import GeometricEdgeFeatures, ScaleEdgeFeatures

def getDataTransforms(args):
    t_lookup = {
        "FaceToEdge": (FaceToEdge, lambda ob: 0),
        "GenerateMeshNormals": (GenerateMeshNormals, lambda ob: 0),
        "TwoHop": (TwoHop, lambda ob: 0),
        "PointPairFeatures": (PointPairFeatures, lambda ob: 4),
        "Cartesian": (Cartesian, lambda ob: 3),
        "GeometricEdgeFeatures": (GeometricEdgeFeatures, lambda ob: ob.edge_dim),
        "ScaleEdgeFeatures": (ScaleEdgeFeatures, lambda ob: 0)
    }
    transforms = []
    edge_dim = 0
    for arg in args:
        t = t_lookup[arg["name"]][0](**arg.get("kwargs", {}))
        edge_dim += t_lookup[arg["name"]][1](t)
        
        transforms.append(t)
    
    return Compose(transforms), edge_dim

def addWeightDecay(model, weight_decay=1e-5, skip_list=()):
    """This function excludes certain parameters (e.g. batch norm, and linear biases)
    from being subject to weight decay"""
    decay = []
    no_decay = []
    for name, param in model.named_parameters():
        if not param.requires_grad:
            continue
        if len(param.shape) == 1 or name.endswith(".bias") or name in skip_list:
            no_decay.append(param)
        else:
            decay.append(param)
    return [
        {'params': no_decay, 'weight_decay': 0.0},
        {'params': decay, 'weight_decay': weight_decay}]

####################################################################################################
# Load the config file
defaults = {
    "no_random": False,
    "debug": False,
    "output_path": ".",
    "tensorboard": True,
    "write": True,
    "write_test_predictions": True,
    "single_gpu": False,
    "balance": "unmasked",
    "shuffle": True,
    "weight_method": "dataset",
    "checkpoint_every": 0,
    "eval_every": 2,
    "best_state_metric": "auroc",
    "best_state_metric_threshold": 0.7,
    "best_state_metric_dataset": "validation", 
    "best_state_metric_goal": "max" 
}
ARGS = arg_parser.parse_args()
with open(ARGS.config_file) as FH:
    C = json.load(FH)
# add any missing default args
for key, val in defaults.items():
    if key not in C:
        C[key] = val
# override any explicit args
for key, val in vars(ARGS).items():
    if val is not None:
        C[key] = val

# Set random seed or not
if C["no_random"] or C["debug"]:
    np.random.seed(8)
    torch.manual_seed(0)

# Get run name and path
config = '.'.join(ARGS.config_file.split('.')[:-1])
if ARGS.run_name:
    run_name = ARGS.run_name
else:
    run_name = "{}_{}_{}".format(C.get("run_name", config), datetime.now().strftime("%m.%d.%Y.%H.%M"), np.random.randint(1000))

if ARGS.output_path:
    run_path = ospj(ARGS.output_path, run_name)
else:
    run_path = ospj(C["output_path"], run_name)

if not os.path.exists(run_path) and C["write"]:
    os.makedirs(run_path)

# Set up logging
log_level = logging.DEBUG if C["debug"] else logging.INFO
log_format = '%(levelname)s:    %(message)s'
if C["write"]:
    filename = ospj(run_path, 'run.log')
    logging.basicConfig(format=log_format, filename=filename, level=log_level)
    console = logging.StreamHandler()
    console.setLevel(log_level)
    formatter = logging.Formatter(log_format)
    console.setFormatter(formatter)
    logging.getLogger('').addHandler(console)
else:
    filename = None
    logging.basicConfig(format=log_format, filename=filename, level=log_level)

# Save copy of config to run directory
if C["write"]:
    shutil.copyfile(ARGS.config_file, ospj(run_path, 'config.json'))

# Set checkpoint values
if(C["checkpoint_every"] == 0 or C["checkpoint_every"] > C["epochs"]):
    C["checkpoint_every"] = C["epochs"] # write once at end of training
elif(C["checkpoint_every"] < 0 or C["debug"]):
    C["checkpoint_every"] = False

# Create tensorboard writer
if C["tensorboard"] and C["write"]:
    writer = SummaryWriter(run_path)
else:
    writer = None

####################################################################################################
# Load training/validation data
train_datafiles = [_.strip() for _ in open(ARGS.train_file).readlines()]
valid_datafiles = [_.strip() for _ in open(ARGS.valid_file).readlines()]

remove_mask = (C["balance"] == 'all')

trans_args = C["model"].get("transform_args", [])
transform, edge_dim = getDataTransforms(trans_args)
feature_mask = C.get("feature_mask", None)

train_dataset, transforms, train_info = loadDataset(train_datafiles, C["nc"], C["labels_key"], C["data_dir"],
        cache_dataset=C.get("cache_dataset", False),
        balance=C["balance"],
        remove_mask=remove_mask,
        scale=True,
        pre_transform=transform,
        feature_mask=feature_mask
    )
valid_dataset, _, valid_info = loadDataset(valid_datafiles, C["nc"], C["labels_key"], C["data_dir"],
        cache_dataset=C.get("cache_dataset", False),
        balance='unmasked',
        remove_mask=False,
        scale=True,
        feature_mask=feature_mask,
        **transforms
    )

# save scaler to file
pickle.dump(transforms["scaler"], open(ospj(run_path, 'scaler.pkl'), "wb"))

if torch.cuda.device_count() <= 1 or C["single_gpu"] or C["debug"]:
    # prepate data for single GPU or CPU 
    DL_tr = DataLoader(train_dataset, batch_size=C["batch_size"], shuffle=C["shuffle"], pin_memory=True)
    DL_vl = DataLoader(valid_dataset, batch_size=1, shuffle=False, pin_memory=True) 
else:
    # prepare data for parallelization over multiple GPUs
    DL_tr = DataListLoader(train_dataset, batch_size=torch.cuda.device_count()*C["batch_size"], shuffle=C["shuffle"], pin_memory=True)
    DL_vl = DataListLoader(valid_dataset, batch_size=1, shuffle=False, pin_memory=True)

####################################################################################################
# Create the model we'll be training.
nF = train_info['num_features']
if C["model"]["name"] == "NetConvPool":
    C["model"]["kwargs"]["edge_dim"] = edge_dim
    model = NetConvPool(nF, C['nc'], **C["model"]["kwargs"])
elif C["model"]["name"] == "PointNetPP":
    model = PointNetPP(nF, C['nc'], **C["model"]["kwargs"])
elif C["model"]["name"] == "MultiBranchNet":
    #C["model"]["kwargs"]["kwargs2"]["edge_dim"] = edge_dim
    model = MultiBranchNet(nF, C['nc'], **C["model"]["kwargs"])
elif C["model"]["name"] == "FFNet":
    model = FFNet(nF, C['nc'])

# debugging: log model parameters
logging.debug("Model Summary:")
for name, param in model.named_parameters():
    if param.requires_grad:
        logging.debug("%s: %s", name, param.data.shape)

# Set up multiple GPU utilization
device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')
if torch.cuda.device_count() <= 1 or C["single_gpu"] or C["debug"]:
    logging.info("Running model on device %s.", device)
else:
    model = DataParallel(model)
    logging.info("Distributing model on %d gpus with root %s", torch.cuda.device_count(), device)
model = model.to(device)

####################################################################################################
### Set up optimizer, scheduler and loss ###########################################################

# optimizer
model_parameters = addWeightDecay(model, C["optimizer"]["kwargs"]["weight_decay"])

if(C["optimizer"]["name"] == "adam"):
    optimizer = torch.optim.Adam(model_parameters, **C["optimizer"]["kwargs"])
elif(C["optimizer"]["name"] == "sgd"):
    optimizer = torch.optim.SGD(model_parameters, **C["optimizer"]["kwargs"])
logging.info("configured optimizer: %s", C["optimizer"]["name"])

# scheduler
if(C["scheduler"]["name"] == "ReduceLROnPlateau"):
    scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer, **C["scheduler"]["kwargs"])
elif(C["scheduler"]["name"] == "ExponentialLR"):
    scheduler = torch.optim.lr_scheduler.ExponentialLR(optimizer, **C["scheduler"]["kwargs"])
elif(C["scheduler"]["name"] == "OneCycleLR"):
    nsteps = int(np.ceil(len(train_datafiles)/(C["batch_size"]*max(1, torch.cuda.device_count()))))
    scheduler = torch.optim.lr_scheduler.OneCycleLR(optimizer, C["scheduler"]["max_lr"], epochs=C["epochs"], steps_per_epoch=nsteps, **C["scheduler"]["kwargs"])
else:
    scheduler = None
logging.info("configured learning rate scheduler: %s", C["scheduler"]["name"])

# Loss function
criterion = torch.nn.functional.cross_entropy

####################################################################################################
### Do the training ################################################################################
evaluator = Evaluator(model, C["nc"], device=device, post_process=torch.nn.Softmax(dim=-1))
trainer = Trainer(model, C["nc"], optimizer, criterion, device, scheduler, evaluator,
    checkpoint_path=run_path,
    writer=writer,
    quiet=False
)

# determine how to weight classes
if C["weight_method"] == 'dataset':
    opt_kw = {
        "weight": classWeights(DL_tr, C["nc"], device),
        "use_weight": True
    }
elif C["weight_method"] == 'batch':
    opt_kw = {
        "weight": None,
        "use_weight": True
    }
else:
    opt_kw = {
        "use_weight": False
    }

# train
trainer.train(C["epochs"], DL_tr, DL_vl,
    optimizer_kwargs=opt_kw,
    checkpoint_every=C["checkpoint_every"],
    eval_every=C["eval_every"],
    best_state_metric=C["best_state_metric"],
    best_state_metric_threshold=C["best_state_metric_threshold"],
    best_state_metric_dataset=C["best_state_metric_dataset"], 
    best_state_metric_goal=C["best_state_metric_goal"],
    params_to_write=["module.crf1.log_alpha", "module.crf1.log_beta", "module.crf1.log_sigmasq"]
)

# Write final training predictions to file
if C["write"]:
    if trainer.best_state is not None:
        # load the best model
        model.load_state_dict(trainer.best_state)
        logging.info("Loaded best state for model evaluation")
    train_out = evaluator.eval(DL_tr, use_masks=True, eval_mode=True)
    valid_out = evaluator.eval(DL_vl, use_masks=True, eval_mode=True)
    np.savez_compressed(ospj(run_path, "training_set_predictions.npz"), Y=train_out['y'], P=train_out['output'])
    np.savez_compressed(ospj(run_path, "validation_set_predictions.npz"), Y=valid_out['y'], P=valid_out['output'])

####################################################################################################

# Evaluate validation dataset
if C["write"] and C["write_test_predictions"]:
    prediction_path = ospj(run_path, "predictions")
    if not os.path.exists(prediction_path):
        os.mkdir(prediction_path)
    lw = max([len(_)-len("_protein_data.npz") for _ in valid_datafiles] + [len("Protein Identifier")])
    use_header = True
    history = trainer.metrics_history
    if ("train" in history) and ("threshold" in history["train"]) and (trainer.best_epoch is not None):
        threshold = trainer.getHistory('train', 'threshold', trainer.best_epoch)
    elif ("train" in history) and ("threshold" in history["train"]):
        threshold = trainer.getHistory('train', 'threshold', -1)
    else:
        threshold = 0.5
    
    val_out = evaluator.eval(DL_vl, use_masks=False, batchwise=True, return_masks=True, return_predicted=True, return_batches=True, xtras=['pos', 'face'], threshold=threshold, eval_mode=True)
    for i in range(val_out['num_batches']):
        name = valid_datafiles[i].replace("_protein_data.npz", "")
        
        # compute metrics
        y, prob, mask = val_out['y'][i], val_out['output'][i], val_out['masks'][i]
        batch = val_out['batches'][i]
        metrics = evaluator.getMetrics(y, prob, mask, batch, threshold=threshold)
        reportMetrics({"validation predictions": metrics}, label=("Protein Identifier", name), label_width=lw, header=use_header)
        
        # write predictions to file
        if C["write"]:
            np.savez_compressed(ospj(prediction_path, "%s_predict.npz" % (name)), Y=y, Ypr=val_out['predicted_y'][i], P=prob, V=val_out['pos'][i], F=val_out['face'][i].T)
        use_header=False
