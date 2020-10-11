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

# dataset options
arg_parser.add_argument("--balance", type=str, choices=['balanced', 'non-masked', 'all'],
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

# Third party modules
import numpy as np
import torch
from torch.utils.tensorboard import SummaryWriter
from torch_geometric.nn import DataParallel
from torch_geometric.data import DataLoader, DataListLoader

# Geobind modules
from geobind.nn.utils import ClassificationDatasetMemory
from geobind.nn import Trainer, Evaluator
from geobind.nn.models import NetConvEdgePool, PointNetPP, MultiBranchNet
from geobind.nn.metrics import reportMetrics

####################################################################################################

# Load the config file
defaults = {
    "no_random": False,
    "debug": False,
    "output_path": ".",
    "checkpoint_every": 0,
    "tensorboard": True,
    "write": True,
    "write_test_predictions": True,
    "single_gpu": False,
    "balance": "balanced",
    "shuffle": True,
    "eval_every": 2
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
print(C)

# Set random seed or not
if C["no_random"] or C["debug"]:
    np.random.seed(8)
    torch.manual_seed(0)

# Get run name and path
config = '.'.join(ARGS.config_file.split('.')[:-1])
run_name = "{}_{}_{}".format(C.get("run_name", config), datetime.now().strftime("%m.%d.%Y.%H.%M"), np.random.randint(1000))
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
### Load training/validation data ##################################################################

train_data = [_.strip() for _ in open(ARGS.train_file).readlines()]
valid_data = [_.strip() for _ in open(ARGS.valid_file).readlines()]

remove_mask = (C["balance"] == 'all')
train_dataset = ClassificationDatasetMemory(
        train_data, C["nc"], C["data_dir"],
        balance=C["balance"],
        remove_mask=remove_mask,
        scale=True
    )

valid_dataset = ClassificationDatasetMemory(
        valid_data, C["nc"], C["data_dir"],
        balance='non-masked',
        remove_mask=False,
        scale=True,
        scaler=train_dataset.scaler
    )

if torch.cuda.device_count() <= 1 or C["single_gpu"]:
    # prepate data for single GPU or CPU 
    DL_tr = DataLoader(train_dataset, batch_size=C["batch_size"], shuffle=C["shuffle"], pin_memory=True)
    DL_vl = DataLoader(valid_dataset, batch_size=1, shuffle=False, pin_memory=True) 
else:
    # prepare data for parallelization over multiple GPUs
    DL_tr = DataListLoader(train_dataset, batch_size=torch.cuda.device_count()*C["batch_size"], shuffle=C["shuffle"], pin_memory=True)
    DL_vl = DataListLoader(valid_dataset, batch_size=1, shuffle=False, pin_memory=True)

####################################################################################################

# Create the model we'll be training.
nF = train_dataset.num_node_features
if C["model"]["name"] == "Net_Conv_EdgePool":
    model = NetConvEdgePool(nF, C['nc'], **C["model"]["kwargs"])
elif C["model"]["name"] == "PointNetPP":
    model = PointNetPP(nF, C['nc'], **C["model"]["kwargs"])
elif C["model"]["name"] == "MultiBranchNet":
    model = MultiBranchNet(nF, C['nc'], **C["model"]["kwargs"])


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
if(C["optimizer"]["name"] == "adam"):
    optimizer = torch.optim.Adam(model.parameters(), **C["optimizer"]["kwargs"])
elif(C["optimizer"]["name"] == "sgd"):
    optimizer = torch.optim.SGD(model.parameters(), **C["optimizer"]["kwargs"])
logging.info("configured optimizer: %s", C["optimizer"]["name"])

# scheduler
if(C["scheduler"]["name"] == "ReduceLROnPlateau"):
    scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer, **C["scheduler"]["kwargs"])
elif(C["scheduler"]["name"] == "ExponentialLR"):
    scheduler = torch.optim.lr_scheduler.ExponentialLR(optimizer, **C["scheduler"]["kwargs"])
elif(C["scheduler"]["name"] == "OneCycleLR"):
    nsteps = len(train_data)//C["batch_size"]
    scheduler = torch.optim.lr_scheduler.OneCycleLR(optimizer, epochs=C["epochs"], steps_per_epoch=nsteps, **C["scheduler"]["kwargs"])
else:
    scheduler = None
logging.info("configured learning rate scheduler: %s", C["scheduler"]["name"])

# Loss function
criterion = torch.nn.functional.cross_entropy

####################################################################################################
### Do the training ################################################################################

evaluator = Evaluator(model, device=device, post_process=torch.nn.Softmax(dim=-1))
trainer = Trainer(model, optimizer, criterion, device, scheduler, evaluator,
    checkpoint_path=run_path,
    writer=writer,
    quiet=False
)
if C["debug"]:
    stats = trainer.train(C["epochs"], DL_tr, DL_vl, checkpoint_every=C["checkpoint_every"], eval_every=C["eval_every"], debug=True)
    logging.basicConfig(level=logging.INFO)
    # plot
    plt.plot(stats["current"], label='current')
    plt.plot(stats["peak"], label='peak')
    for x in stats["epoch_start"]:
        plt.axvline(x, color='k')
    plt.xlabel("iteration")
    plt.ylabel("GPU Memory Usage (MB)")
    plt.legend()
    plt.savefig("{}_memusage.png".format(run_name))
else:
    trainer.train(C["epochs"], DL_tr, DL_vl, checkpoint_every=C["checkpoint_every"], eval_every=C["eval_every"], debug=False)

# Write final training predictions to file
if C["write"]:
    y_gt, prob = evaluator.eval(DL_tr, use_mask=False)
    np.savez_compressed(ospj(run_path, "training_set_predictions.npz"), Y=y_gt, P=prob)

####################################################################################################

# Evaluate validation dataset
if C["write_test_predictions"]:
    prediction_path = ospj(run_path, "predictions")
    if not os.path.exists(prediction_path):
        os.mkdir(prediction_path)
    val_out = evaluator.eval(DL_vl, use_mask=False, batchwise=True, return_masks=True)
    lw = max([len(_)-len("_protein_data.npz") for _ in valid_data])
    use_header = True
    threshold = trainer.metrics_history['train']['threshold'][-1] if "train" in trainer.metrics_history else 0.5
    
    for i, _ in enumerate(val_out):
        y, prob, mask = _
        name = valid_data[i].replace("_protein_data.npz", "")
        
        # compute metrics
        metrics = evaluator.getMetrics(y[mask], prob[mask], threshold=threshold)
        reportMetrics({"validation predictions": metrics}, label=("Protein Identifier", name), label_width=lw, header=use_header)
        
        # write predictions to file
        if C["write"]:
            y_pr = (prob[:,1] >= threshold)
            np.savez_compressed(ospj(prediction_path, "%s_predict.npz" % (name)), Ypr=y_pr, P=prob)
        use_header=False
