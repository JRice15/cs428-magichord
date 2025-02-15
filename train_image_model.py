"""
This file is to train the model that takes in individual video frames, and outputs
predicted probabilities of what fret is held down for each string

A present, the models used are pretty large, and should probably be trained
on a GPU
"""

import argparse
import json
import os
import time
import pprint
import shutil

import keras
import numpy as np
import tensorflow as tf
from keras import backend as K
from keras.callbacks import (History, LearningRateScheduler, ModelCheckpoint,
                             ReduceLROnPlateau, EarlyStopping)
from keras.models import Model
from keras.optimizers import Adam

from src.cv_helpers import *
from src.image_models import make_model, fret_accuracy
from src.save_stats import save_history
from src.load_data import load_all_data
from test_image_model import test_im_model


parser = argparse.ArgumentParser()
parser.add_argument("--name",required=True,help="name to save the model under")
parser.add_argument("--nodisplay",action="store_true")
parser.add_argument("--test",action="store_true",help="load a small portion of the data for a quick test run")
parser.usage = parser.format_help()
args = parser.parse_args()

if args.test:
    args.name += "_test"

class TrainConfig:

    def __init__(self, epochs, model, batchsize, lr, lr_sched_freq, 
            lr_sched_factor, loss):
        self.epochs = epochs
        self.model = model
        self.batchsize = batchsize
        self.lr = lr
        self.lr_sched_freq = lr_sched_freq
        self.lr_sched_factor = lr_sched_factor
        self.loss = loss
        pprint.pprint(vars(self))
    
    def __str__(self):
        return str(vars(self))
    
    def write_to_file(self,filename):
        with open(filename, "a") as f:
            f.write("\n" + str(self) + "\n\n")

with open("model_config.json", "r") as f:
    config_dict = json.load(f)

config = TrainConfig(**config_dict)

"""
load data

train data is loaded from data/image_model_train, while test data is from data/inference_model_train
"""
data = load_all_data("data/image_model_train", num_splits=1, split_amount=0.1,
            display=(not args.nodisplay), do_test=args.test)
xtrain, xval, _, ytrain, yval, _ = data

# shuffle train set
shuffle_inds = np.random.permutation(len(xtrain))
xtrain = xtrain[shuffle_inds]
ytrain = ytrain[shuffle_inds]

print(len(xtrain), "training images,", len(xval), "validation,")

# for testing later
xtrain_short = xtrain[:20].copy()
ytrain_short = ytrain[:20].copy()

img_shape = xtrain[0].shape

"""
store data in sets of batches inside the temp folder, cuz its too big to have loaded all at once
"""

DATA_TEMP_DIR = "data/.temp/"

print("Preparing training batches in", DATA_TEMP_DIR)
try:
    shutil.rmtree(DATA_TEMP_DIR)
except FileNotFoundError:
    pass

os.makedirs(DATA_TEMP_DIR+"x/")
os.makedirs(DATA_TEMP_DIR+"y/")

x = []
y = []
for i in range(0, len(xtrain)-config.batchsize, config.batchsize):
    x.append( xtrain[i:i+config.batchsize] )
    y.append( ytrain[i:i+config.batchsize] )
    # save batches in sets of 50
    if len(x) >= 50:
        np.save(DATA_TEMP_DIR+"x/batches_"+str(i), x)
        np.save(DATA_TEMP_DIR+"y/batches_"+str(i), y)
        x = []
        y = []

if len(x) > 0:
    np.save(DATA_TEMP_DIR+"x/batches_"+str(i), x)
    np.save(DATA_TEMP_DIR+"y/batches_"+str(i), y)
    del x, y

# we can now delete the training data to free memory
del xtrain, ytrain
import gc
gc.collect()

"""
make model
"""

# get the loss function
lossname = config.loss.lower()
print("Using loss", lossname)
categorical = False # whether output is regression or categorization
if lossname == "mse":
    loss = keras.losses.mean_squared_error
elif lossname == "huber":
    loss = keras.losses.Huber(delta=1)
elif lossname == "mae":
    loss = keras.losses.mean_absolute_error
elif lossname == "sparsecategoricalcrossentropy":
    loss = keras.losses.SparseCategoricalCrossentropy()
    categorical = True
else:
    raise ValueError("No such loss '{}'".format(config.loss))

model = make_model(config.model, img_shape, output_confidences=categorical)

model.summary()
keras.utils.plot_model(model, show_shapes=True, dpi=192)

if categorical:
    metrics = ["sparse_categorical_accuracy"]
else:
    metrics = [fret_accuracy(), "mae"]

model.compile(
    loss=loss,
    optimizer=Adam(config.lr),
    metrics=metrics,
)

output_shape = model.get_output_shape_at(-1)[:1] # except batch size

"""
train model
"""

def train_gen():
    """
    load batched data from temp
    """
    while True:
        for path in os.listdir(DATA_TEMP_DIR+"x/"):
            X = np.load(DATA_TEMP_DIR+"x/"+path)
            Y = np.load(DATA_TEMP_DIR+"y/"+path)
            assert len(X) == len(Y)
            for i in range(len(X)):
                #showim(X[i][0])
                yield X[i], Y[i]


def lr_sched(epoch, lr=None):
    if lr is None:
        if epoch % config.lr_sched_freq == 0:
            print("Decreasing learning rate to", lr)
        exp = epoch // config.lr_sched_freq
        lr = config.lr * (config.lr_sched_factor ** exp)
    elif epoch == 0:
        pass
    elif epoch % config.lr_sched_freq == 0:
        lr = lr * config.lr_sched_factor
        print("Decreasing learning rate to", lr)
    return lr

os.makedirs("models/", exist_ok=True)
callbacks = [
    History(),
    LearningRateScheduler(lr_sched),
    ModelCheckpoint("models/"+args.name+".hdf5", save_best_only=True, verbose=1, period=1),
    EarlyStopping(monitor='val_loss', verbose=1, patience=int(config.lr_sched_freq * 1.5))
]

start = time.time()
try:
    H = model.fit(
        train_gen(),
        validation_data=(xval, yval),
        batch_size=config.batchsize,
        epochs=config.epochs,
        verbose=1,
        steps_per_epoch=int(6400/config.batchsize), # 6400 examples each epoch, which is 200 batches with a 32 batchsize
        callbacks=callbacks,
    )
except KeyboardInterrupt:
    print("\nManual early stopping")
    H = callbacks[0]
end = time.time()

step = max(1, len(H.history['loss']) // 6)
save_history(H, args.name, end-start, config, marker_step=step)

del xval, yval

"""
testing
"""

test_im_model(args.name, xtrain_short=xtrain_short, do_test=args.test,
    ytrain_short=ytrain_short, nodisplay=args.nodisplay, summary=False)
