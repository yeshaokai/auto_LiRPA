import argparse, random, pickle, os, pdb, time, math
import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
from examples.sequence.lstm import LSTM
from examples.sequence.data_utils import load_data
from examples.language.data_utils import get_batches
from auto_LiRPA.utils import AverageMeter, logger
from auto_LiRPA.perturbations import PerturbationLpNorm
from auto_LiRPA.bound_general import BoundGeneral

parser = argparse.ArgumentParser()
parser.add_argument("--seed", type=int, default=0)
parser.add_argument("--device", type=str, default="cuda", choices=["cuda", "cpu"])
parser.add_argument("--norm", type=int, default=2)
parser.add_argument("--eps", type=float, default=0.1)
parser.add_argument("--num_epochs", type=int, default=20)  
parser.add_argument("--batch_size", type=int, default=128)
parser.add_argument("--num_slices", type=int, default=8)
parser.add_argument("--hidden_size", type=int, default=64)
parser.add_argument("--num_labels", type=int, default=10) 
parser.add_argument("--input_size", type=int, default=784)
parser.add_argument("--lr", type=float, default=5e-3)
parser.add_argument("--dir", type=str, default="model", help="directory to load or save the model")
parser.add_argument("--num_epochs_warmup", type=int, default=1, help="number of epochs for the warmup stage when eps is linearly increased from 0 to the full value")
parser.add_argument("--log_interval", type=int, default=10, help="interval of printing the log during training")
args = parser.parse_args()   

def step(model, ptb, batch, eps=args.eps, train=False):
    X, y = model.get_input(batch)
    logits = model.core(X)
    ptb.set_eps(eps)
    logits_robust = model.core.compute_worst_logits(ptb=ptb, x=X, y=y, IBP=True)
        
    preds = torch.argmax(logits, dim=1)
    acc = torch.sum((preds == y).to(torch.float32)) / len(batch)
    preds_robust = torch.argmax(logits_robust, dim=1)
    acc_robust = torch.sum((preds_robust == y).to(torch.float32)) / len(batch)
    loss_fct = nn.CrossEntropyLoss()
    loss = 0.8 * loss_fct(logits_robust, y) + 0.2 * loss_fct(logits, y)

    if train:
        loss.backward()

    return acc.detach(), acc_robust.detach(), loss.detach()

data_train, data_test = load_data()
logger.info("Dataset sizes: {}/{}".format(len(data_train), len(data_test)))

random.seed(args.seed)
np.random.seed(args.seed)
torch.manual_seed(args.seed)
torch.cuda.manual_seed_all(args.seed)

model = LSTM(args).to(args.device)   
test_batches = get_batches(data_test, args.batch_size) 
X, y = model.get_input(test_batches[0])
model.core = BoundGeneral(model.core, (X,))
ptb = PerturbationLpNorm(norm=args.norm, eps=args.eps) 
optimizer = model.build_optimizer()

avg_acc, avg_acc_robust, avg_loss = avg = [AverageMeter() for i in range(3)]

def train(epoch):
    model.train()
    train_batches = get_batches(data_train, args.batch_size)
    for a in avg: a.reset()       
    eps_inc_per_step = 1.0 / (args.num_epochs_warmup * len(train_batches))
    for i, batch in enumerate(train_batches):
        eps = args.eps * min(eps_inc_per_step * ((epoch - 1) * len(train_batches) + i + 1), 1.0)
        acc, acc_robust, loss = res = step(model, ptb, batch, eps=eps, train=True)
        torch.nn.utils.clip_grad_norm_(model.core.parameters(), 5.0)
        optimizer.step()
        optimizer.zero_grad()       
        for k in range(3):
            avg[k].update(res[k], len(batch))  
        if (i + 1) % args.log_interval == 0:
            logger.info("Epoch {}, training step {}/{}: acc {:.3f}, acc_robust {:.3f}, loss {:.3f}, eps {:.3f}".format(
                epoch, i + 1, len(train_batches), avg_acc.avg, avg_acc_robust.avg, avg_loss.avg, eps))
    model.save(epoch)

def infer(epoch, batches, type):
    model.eval()
    for a in avg: a.reset()    
    for i, batch in enumerate(batches):
        acc, acc_robust, loss = res = step(model, ptb, batch)
        for k in range(3):
            avg[k].update(res[k], len(batch))                 
    logger.info("Epoch {}, {}: acc {:.3f}, acc_robust {:.3f}, loss {:.5f}".format(
        epoch, type, avg_acc.avg, avg_acc_robust.avg, avg_loss.avg))

def main():
    for t in range(model.checkpoint, args.num_epochs):
        train(t + 1)
        infer(t + 1, test_batches, "test")
    if model.checkpoint == args.num_epochs:
        infer(args.num_epochs, test_batches, "test")

if __name__ == "__main__":
    main()
