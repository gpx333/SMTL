import torch, time, os, random
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import numpy as np
import scipy.io as sio
from backbone_bilevel import AMTLmodel, AMTLmodel_new
from utils import *

from create_dataset import NYUv2

import argparse

torch.manual_seed(0)
random.seed(0)
np.random.seed(0)

def parse_args():
    parser = argparse.ArgumentParser(description= 'AMTL on NYUv2')
    parser.add_argument('--gpu_id', default='0', help='gpu_id') 
    parser.add_argument('--model', default='AMTL', type=str, help='AMTL, AMTL_new')
    parser.add_argument('--aug', type=str, default='False', help='data augmentation')
    parser.add_argument('--train_mode', default='train', type=str, help='trainval, train')
    parser.add_argument('--total_epoch', default=200, type=int, help='training epoch')
    # for AMTL
    parser.add_argument('--version', default='v1', type=str, help='v1 (a1+a2=1), v2 (0<=a<=1), v3 (gumbel softmax)')
    return parser.parse_args()


params = parse_args()
print(params)

os.environ["CUDA_VISIBLE_DEVICES"] = params.gpu_id

dataset_path = '/data/dataset/nyuv2/'

def build_model():
    if params.model == 'AMTL':
        batch_size = 2
        model = AMTLmodel(version=params.version).cuda()
    elif params.model == 'AMTL_new':
        batch_size = 14
        model = AMTLmodel_new(version=params.version).cuda()
    else:
        print("No correct model parameter!")
        exit()
    return model, batch_size

model, batch_size = build_model()

nyuv2_train_set = NYUv2(root=dataset_path, mode=params.train_mode, augmentation=params.aug)
nyuv2_val_test = NYUv2(root=dataset_path, mode='val', augmentation='False')
nyuv2_test_set = NYUv2(root=dataset_path, mode='test', augmentation='False')

nyuv2_train_loader = torch.utils.data.DataLoader(
    dataset=nyuv2_train_set,
    batch_size=batch_size,
    shuffle=True,
    num_workers=2,
    pin_memory=True,
    drop_last=True)
    
nyuv2_val_loader = torch.utils.data.DataLoader(
    dataset=nyuv2_train_set,
    batch_size=batch_size,
    shuffle=True,
    num_workers=2,
    pin_memory=True,
    drop_last=True)

nyuv2_test_loader = torch.utils.data.DataLoader(
    dataset=nyuv2_test_set,
    batch_size=batch_size,
    shuffle=False,
    num_workers=2,
    pin_memory=True)


task_num = len(model.tasks)

optimizer = optim.Adam(model.parameters(), lr=1e-4, weight_decay=1e-5)
scheduler = optim.lr_scheduler.StepLR(optimizer, step_size=100, gamma=0.5)

# gate parameter, alpha
class Model_alpha(nn.Module):
    def __init__(self, task_num=3, version='v1'):
        super(Model_alpha, self).__init__()
        # adaptative parameters
        if version == 'v1' or version =='v2':
            # AMTL-v1 and v2
            self.alpha = nn.Parameter(torch.FloatTensor(task_num, 2))
            self.alpha.data.fill_(0.5)   # init 0.5(shared) 0.5(specific)
            # self.alpha.data[:,0].fill_(0)  # shared
            # self.alpha.data[:,1].fill_(1)  # specific
        elif version == 'v3':
            # AMTL-v3, gumbel softmax
            self.alpha = nn.Parameter(torch.FloatTensor(task_num))
            self.alpha.data.fill_(0)
        else:
            print("No correct version parameter!")
            exit()
    
    def get_adaptative_parameter(self):
        return self.alpha

h = Model_alpha(task_num=3, version=params.version).cuda()
h.train()
h_optimizer = torch.optim.Adam(h.parameters(), lr=1e-4)


def set_param(curr_mod, name, param=None, mode='update'):
    if '.' in name:
        n = name.split('.')
        module_name = n[0]
        rest = '.'.join(n[1:])
        for name, mod in curr_mod.named_children():
            if module_name == name:
                return set_param(mod, rest, param, mode=mode)
    else:
        if mode == 'update':
            delattr(curr_mod, name)
            setattr(curr_mod, name, param)
        elif mode == 'get':
            if hasattr(curr_mod, name):
                p = getattr(curr_mod, name)
                return p

print('LOSS FORMAT: SEMANTIC_LOSS MEAN_IOU PIX_ACC | DEPTH_LOSS ABS_ERR REL_ERR | NORMAL_LOSS MEAN MED <11.25 <22.5 <30')
total_epoch = params.total_epoch
train_batch = len(nyuv2_train_loader)
val_batch = len(nyuv2_val_loader)
avg_cost = torch.zeros([total_epoch, 24])
lambda_weight = torch.ones([task_num, total_epoch]).cuda()
for index in range(total_epoch):
    s_t = time.time()
    cost = torch.zeros(24)

    # iteration for all batches
    model.train()
    train_dataset = iter(nyuv2_train_loader)
    val_dataset = iter(nyuv2_val_loader)
    conf_mat = ConfMatrix(model.class_nb)
    for k in range(min(train_batch, val_batch)):
        
        meta_model, _ = build_model()
        meta_model.load_state_dict(model.state_dict())
        
        model_np = {}
        for n, p in meta_model.named_parameters():
            model_np[n] = p
        
        train_data, train_label, train_depth, train_normal = train_dataset.next()
        train_data, train_label = train_data.cuda(non_blocking=True), train_label.long().cuda(non_blocking=True)
        train_depth, train_normal = train_depth.cuda(non_blocking=True), train_normal.cuda(non_blocking=True)

        train_pred = meta_model(train_data, h)

        train_loss = [model_fit(train_pred[0], train_label, 'semantic'),
                      model_fit(train_pred[1], train_depth, 'depth'),
                      model_fit(train_pred[2], train_normal, 'normal')]
        loss_train = torch.zeros(3).cuda()
        for i in range(3):
            loss_train[i] = train_loss[i]
        loss_sum = torch.sum(loss_train*lambda_weight[:, index])
        
        meta_model.zero_grad()
        grads = torch.autograd.grad(loss_sum, (meta_model.parameters()), create_graph=True)
        
        for g_index, name in enumerate(model_np.keys()):
            p = set_param(meta_model, name, mode='get')
            if grads[g_index] == None:
                print(g_index, name, grads[g_index])
                continue
            p_fast = p - 1e-4 * grads[g_index]
            set_param(meta_model, name, param=p_fast, mode='update')
            model_np[name] = p_fast
        del grads
        del model_np
        
        # update outer loop
        val_data, val_label, val_depth, val_normal = val_dataset.next()
        val_data, val_label = val_data.cuda(non_blocking=True), val_label.long().cuda(non_blocking=True)
        val_depth, val_normal = val_depth.cuda(non_blocking=True), val_normal.cuda(non_blocking=True)
        valid_pred = meta_model(val_data, h)
        valid_loss = [model_fit(valid_pred[0], val_label, 'semantic'),
                      model_fit(valid_pred[1], val_depth, 'depth'),
                      model_fit(valid_pred[2], val_normal, 'normal')]
        loss_val = torch.zeros(3).cuda()
        for i in range(3):
            loss_val[i] = valid_loss[i]
        loss_all = torch.sum(loss_val*lambda_weight[:, index])
        h_optimizer.zero_grad()
        loss_all.backward()
        h_optimizer.step()
        
        # update inner loop
        train_pred = model(train_data, h)
        train_loss = [model_fit(train_pred[0], train_label, 'semantic'),
                      model_fit(train_pred[1], train_depth, 'depth'),
                      model_fit(train_pred[2], train_normal, 'normal')]
        loss_final = torch.zeros(3).cuda()
        for i in range(3):
            loss_final[i] = train_loss[i]
        loss = torch.sum(loss_final*lambda_weight[:, index])
        optimizer.zero_grad()
        loss.backward()
        optimizer.step() 

        # accumulate label prediction for every pixel in training images
        conf_mat.update(train_pred[0].argmax(1).flatten(), train_label.flatten())

        cost[0] = train_loss[0].item()
        cost[3] = train_loss[1].item()
        cost[4], cost[5] = depth_error(train_pred[1], train_depth)
        cost[6] = train_loss[2].item()
        cost[7], cost[8], cost[9], cost[10], cost[11] = normal_error(train_pred[2], train_normal)
        avg_cost[index, :12] += cost[:12] / train_batch

    # compute mIoU and acc
    avg_cost[index, 1], avg_cost[index, 2] = conf_mat.get_metrics()

    # evaluating test data
    model.eval()
    conf_mat = ConfMatrix(model.class_nb)
    with torch.no_grad():  # operations inside don't track history
        val_dataset = iter(nyuv2_test_loader)
        val_batch = len(nyuv2_test_loader)
        for k in range(val_batch):
            val_data, val_label, val_depth, val_normal = val_dataset.next()
            val_data, val_label = val_data.cuda(non_blocking=True), val_label.long().cuda(non_blocking=True)
            val_depth, val_normal = val_depth.cuda(non_blocking=True), val_normal.cuda(non_blocking=True)
            val_pred = model.predict(val_data, h)
            val_loss = [model_fit(val_pred[0], val_label, 'semantic'),
                         model_fit(val_pred[1], val_depth, 'depth'),
                         model_fit(val_pred[2], val_normal, 'normal')]

            conf_mat.update(val_pred[0].argmax(1).flatten(), val_label.flatten())

            cost[12] = val_loss[0].item()
            cost[15] = val_loss[1].item()
            cost[16], cost[17] = depth_error(val_pred[1], val_depth)
            cost[18] = val_loss[2].item()
            cost[19], cost[20], cost[21], cost[22], cost[23] = normal_error(val_pred[2], val_normal)
            avg_cost[index, 12:] += cost[12:] / val_batch

        # compute mIoU and acc
        avg_cost[index, 13], avg_cost[index, 14] = conf_mat.get_metrics()
    
    scheduler.step()
    e_t = time.time()
    if params.model == 'AMTL' or params.model == 'AMTL_new':
        alpha = h.get_adaptative_parameter()
        for i in range(task_num):
            if params.version == 'v1':
                print(alpha[i], F.softmax(alpha[i], 0))   # AMTL-v1, alpha_1 + alpha_2 = 1
            elif params.version == 'v2':
                print(alpha[i], torch.exp(alpha[i]) / (1 + torch.exp(alpha[i])))  # AMTL-v2, 0 <= alpha <= 1
            elif params.version == 'v3':
                # below for AMTL-v3, gumbel softmax
                temp = torch.sigmoid(alpha[i])
                temp_alpha = torch.stack([1-temp, temp])
                print(i, temp_alpha)
            else:
                print("No correct version parameter!")
                exit()
    print('Epoch: {:04d} | TRAIN: {:.4f} {:.4f} {:.4f} | {:.4f} {:.4f} {:.4f} | {:.4f} {:.4f} {:.4f} {:.4f} {:.4f} {:.4f} ||'
        'TEST: {:.4f} {:.4f} {:.4f} | {:.4f} {:.4f} {:.4f} | {:.4f} {:.4f} {:.4f} {:.4f} {:.4f} {:.4f} || {:.4f}'
        .format(index, avg_cost[index, 0], avg_cost[index, 1], avg_cost[index, 2], avg_cost[index, 3],
                avg_cost[index, 4], avg_cost[index, 5], avg_cost[index, 6], avg_cost[index, 7], avg_cost[index, 8],
                avg_cost[index, 9], avg_cost[index, 10], avg_cost[index, 11], avg_cost[index, 12], avg_cost[index, 13],
                avg_cost[index, 14], avg_cost[index, 15], avg_cost[index, 16], avg_cost[index, 17], avg_cost[index, 18],
                avg_cost[index, 19], avg_cost[index, 20], avg_cost[index, 21], avg_cost[index, 22], avg_cost[index, 23], e_t-s_t))
    