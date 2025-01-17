import torch, time, os, random
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import numpy as np
import scipy.io as sio
from backbone import DeepLabv3, Cross_Stitch, MTANDeepLabv3, AdaShare, SMTLmodel, SMTLmodel_new
from nddr_cnn import NDDRCNN
from afa import AFANet
from tqdm import tqdm

from create_dataset_taskonomy import Taskonomy, data_prefetcher
from utils_taskonomy import compute_loss, PerformanceMeter

from torch.cuda.amp import autocast, GradScaler

import argparse

torch.manual_seed(0)
random.seed(0)
np.random.seed(0)

def parse_args():
    parser = argparse.ArgumentParser(description= 'SMTL for Taskonomy-small')
    parser.add_argument('--model', default='DMTL', type=str, help='DMTL, CROSS, MTAN, AdaShare, NDDRCNN, AFA, SMTL, SMTL_new')
    parser.add_argument('--aug', action='store_true', default=False, help='data augmentation')
    parser.add_argument('--task_index', default=10, type=int, help='for STL: 0,1,2,3,4')
    parser.add_argument('--gpu_id', default='0', help='gpu_id') 
    parser.add_argument('--total_epoch', default=200, type=int, help='training epoch')
    # for SMTL
    parser.add_argument('--version', default='v1', type=str, help='v1 (a1+a2=1), v2 (0<=a<=1), v3 (gumbel softmax)')
    return parser.parse_args()

params = parse_args()
print(params)

os.environ["CUDA_VISIBLE_DEVICES"] = params.gpu_id

dataset_path = '/data/baijiongl/taskonomy-tiny/'
tasks = ['seg', 'depth', 'sn', 'keypoint', 'edge']
if params.task_index < len(tasks):
    tasks = [tasks[params.task_index]] 
   
taskonomy_train_set = Taskonomy(dataroot=dataset_path, mode='train', augmentation=params.aug)
taskonomy_test_set = Taskonomy(dataroot=dataset_path, mode='test', augmentation=False)

print('train data', len(taskonomy_train_set))
print('test data', len(taskonomy_test_set))

if params.model == 'DMTL':
    batch_size = 230
    model = DeepLabv3(tasks=tasks).cuda()
elif params.model == 'MTAN':
    batch_size = 120
    model = MTANDeepLabv3(tasks=tasks).cuda()
elif params.model == 'CROSS':
    batch_size = 130
    model = Cross_Stitch(tasks=tasks).cuda()
elif params.model == 'AdaShare':
    batch_size = 180
    model = AdaShare(tasks=tasks).cuda()
elif params.model == 'NDDRCNN':
    batch_size = 100
    model = NDDRCNN(tasks=tasks).cuda()
elif params.model == 'AFA':
    batch_size = 40
    model = AFANet(tasks=tasks).cuda()
elif params.model == 'SMTL':
    batch_size = 100
    model = SMTLmodel(tasks=tasks, version=params.version).cuda()
elif params.model == 'SMTL_new':
    batch_size = 90
    model = SMTLmodel_new(tasks=tasks, version=params.version).cuda()
else:
    print("No correct model parameter!")
    exit()

taskonomy_test_loader = torch.utils.data.DataLoader(
    dataset=taskonomy_test_set,
    batch_size=batch_size,
    shuffle=False,
    num_workers=4,
    pin_memory=True)

taskonomy_train_loader = torch.utils.data.DataLoader(
    dataset=taskonomy_train_set,
    batch_size=batch_size,
    shuffle=False,
    num_workers=4,
    pin_memory=True,
    drop_last=True)

train_prefetcher = data_prefetcher(taskonomy_train_loader)
test_prefetcher = data_prefetcher(taskonomy_test_loader)

optimizer = optim.Adam(model.parameters(), lr=1e-4, weight_decay=1e-5)
scaler = GradScaler()

print('LOSS FORMAT: SEMANTIC_LOSS MEAN_IOU PIX_ACC | DEPTH_LOSS ABS_ERR REL_ERR | NORMAL_LOSS MEAN MED <11.25 <22.5 <30 | KEYPOINT_LOSS ABS_ERR | EDGE_LOSS ABS_ERR')
total_epoch = params.total_epoch
train_batch = len(taskonomy_train_loader)

for epoch in range(total_epoch):
    print('-'*10, epoch)
    s_t = time.time()

    # iteration for all batches
    model.train()
    performance_meter = PerformanceMeter(tasks, dataset_path)
    # for batch_index in tqdm(range(train_batch)):
    for batch_index in range(train_batch):
        # if batch_index > 1:
        #     break
        
        train_data, train_gt_dict = train_prefetcher.next()
        train_data = train_data.cuda()
        # train_gt_dict = train_gt_dict.cuda()
        
        optimizer.zero_grad()
        with autocast():
            train_pred = model.forward(train_data)
            loss_train = compute_loss(train_pred, train_gt_dict, dataset_path)
                
        scaler.scale(sum(loss_train)).backward()
        scaler.step(optimizer)
        scaler.update()
        
        performance_meter.update(train_pred, train_gt_dict)
    eval_results_train = performance_meter.get_score()
    print('TRAIN:', eval_results_train)

    # evaluating test data
    model.eval()
    with torch.no_grad():  # operations inside don't track history
        val_batch = len(taskonomy_test_loader)
        performance_meter = PerformanceMeter(tasks, dataset_path)
        for k in range(val_batch):
            # if k > 1:
            #     break
            
            val_data, val_gt_dict = test_prefetcher.next()
            val_data = val_data.cuda()
            # val_gt_dict = val_gt_dict.cuda()

            val_pred = model.predict(val_data)
            performance_meter.update(val_pred, val_gt_dict)
        eval_results_val = performance_meter.get_score()
        if params.model == 'SMTL' or params.model == 'SMTL_new':
            alpha = model.get_adaptative_parameter()
            for i in range(len(tasks)):
                if params.version == 'v1':
                    print(alpha[i], F.softmax(alpha[i], 0))   # SMTL-v1, alpha_1 + alpha_2 = 1
                elif params.version == 'v2':
                    print(alpha[i], torch.exp(alpha[i]) / (1 + torch.exp(alpha[i])))  # SMTL-v2, 0 <= alpha <= 1
                elif params.version == 'v3':
                    # below for SMTL-v3, gumbel softmax
                    temp = torch.sigmoid(alpha[i])
                    temp_alpha = torch.stack([1-temp, temp])
                    print(i, temp_alpha)
                else:
                    print("No correct version parameter!")
                    exit()   
        print('!!!TEST:', eval_results_val)

    e_t = time.time()
    print('TIME:', e_t-s_t)