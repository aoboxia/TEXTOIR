from importlib import import_module
import torch
import numpy as np
import os
import copy
from torch import nn
from datetime import datetime
from sklearn.metrics import confusion_matrix, accuracy_score
from tqdm import trange, tqdm

from losses import loss_map
from utils.metrics import F_measure
from scipy.stats import norm as dist_model

TIMESTAMP = "{0:%Y-%m-%dT%H-%M-%S/}".format(datetime.now())
train_log_dir = 'logs/train/' + TIMESTAMP
test_log_dir = 'logs/test/'   + TIMESTAMP

        
class DOCManager:
    
    def __init__(self, args, data, model):
        
        self.model = model.model 
        self.optimizer = model.optimizer
        self.device = model.device

        self.data = data 
        self.train_dataloader = data.dataloader.train_labeled_loader
        self.eval_dataloader = data.dataloader.eval_loader 
        self.test_dataloader = data.dataloader.test_loader

        self.loss_fct = loss_map[args.loss_fct]
        
        if args.train:
            self.best_mu_stds = None

        else:
            model_file = os.path.join(args.model_output_dir, 'pytorch_model.bin')
            self.model.load_state_dict(torch.load(model_file))
            self.model.to(self.device)
        
    def train(self, args, data):     
        best_model = None
        wait = 0
        best_eval_score = 0

        for epoch in trange(int(args.num_train_epochs), desc="Epoch"):
            self.model.train()
            tr_loss = 0
            nb_tr_examples, nb_tr_steps = 0, 0
            
            for step, batch in enumerate(tqdm(self.train_dataloader, desc="Iteration")):
                batch = tuple(t.to(self.device) for t in batch)
                input_ids, input_mask, segment_ids, label_ids = batch
                with torch.set_grad_enabled(True):
            
                    loss = self.model(input_ids, segment_ids, input_mask, label_ids, mode='train', loss_fct=self.loss_fct)

                    self.optimizer.zero_grad()
                    loss.backward()
                    self.optimizer.step()
                    
                    tr_loss += loss.item()
                    
                    nb_tr_examples += input_ids.size(0)
                    nb_tr_steps += 1

            loss = tr_loss / nb_tr_steps
            print('train_loss',loss)
            
            mu_stds = self.get_outputs(args, data, self.train_dataloader, get_mu_stds = True)
            y_true, y_pred = self.get_outputs(args, data, self.eval_dataloader, mu_stds = mu_stds)

            eval_score = accuracy_score(y_true, y_pred)
            print('eval_score', eval_score)
            
            if eval_score >= best_eval_score:
                best_model = copy.deepcopy(self.model)
                wait = 0
                best_eval_score = eval_score 
                self.best_mu_stds = mu_stds
            else:
                print(wait)
                wait += 1
                if wait >= args.wait_patient:
                    break
            
            self.test(args, data, show=True)

        self.model = best_model 

        if args.save_model:
            self.model.save_pretrained(args.model_output_dir, save_config=True)
            np.save(os.path.join(args.method_output_dir, 'mu_stds.npy'), self.best_mu_stds)

    def test(self, args, data, show=False):

        y_true, y_pred = self.get_outputs(args, data, self.test_dataloader, mu_stds = self.best_mu_stds)
        cm = confusion_matrix(y_true, y_pred)
        test_results = F_measure(cm)

        acc = round(accuracy_score(y_true, y_pred) * 100, 2)
        test_results['Acc'] = acc
        
        if show:
            print('cm',cm)
            print('results', test_results)

        return test_results

    def get_outputs(self, args, data, dataloader, get_feats = False, get_mu_stds = False, mu_stds = None):
    
        self.model.eval()

        total_labels = torch.empty(0,dtype=torch.long).to(self.device)
        total_logits = torch.empty((0, data.num_labels)).to(self.device)
        total_features = torch.empty((0,args.feat_dim)).to(self.device)

        for batch in tqdm(dataloader, desc="Iteration"):

            batch = tuple(t.to(self.device) for t in batch)
            input_ids, input_mask, segment_ids, label_ids = batch
            with torch.set_grad_enabled(False):

                pooled_output, logits = self.model(input_ids, segment_ids, input_mask)

                total_labels = torch.cat((total_labels,label_ids))
                total_logits = torch.cat((total_logits, logits))
                total_features = torch.cat((total_features, pooled_output))

        if get_feats:
            
            feats = total_features.cpu().numpy()
            return feats 

        else:
            
            if get_mu_stds:

                total_probs = torch.sigmoid(total_logits.detach())
                y_true = total_labels.cpu().numpy()
                y_prob = total_probs.cpu().numpy()

                mu_stds = self.cal_mu_std(y_prob, y_true, data.num_labels)

                return mu_stds
            else:
                y_true = total_labels.cpu().numpy()
                total_probs = torch.sigmoid(total_logits.detach())
                y_prob = total_probs.cpu().numpy()

                if mu_stds is not None:
                    y_pred = self.classify_doc(data, args, y_prob, mu_stds)
            
                return y_true, y_pred



    def classify_doc(self, data, args, y_prob, mu_stds):

        thresholds = {}
        for col in range(data.num_labels):
            threshold = max(0.5, 1 - args.scale * mu_stds[col][1])
            label = data.known_label_list[col]
            thresholds[label] = threshold

        print('DOC_thresholds', thresholds)
        
        y_pred = []
        for p in y_prob:
            max_class = np.argmax(p)
            max_value = np.max(p)
            threshold = max(0.5, 1 - args.scale * mu_stds[max_class][1])
            if max_value > threshold:
                y_pred.append(max_class)
            else:
                y_pred.append(data.unseen_label_id)

        return np.array(y_pred)
    
    def fit(self, prob_pos_X):
        prob_pos = [p for p in prob_pos_X] + [2 - p for p in prob_pos_X]
        pos_mu, pos_std = dist_model.fit(prob_pos)
        return pos_mu, pos_std

    def cal_mu_std(self, y_prob, trues, num_labels):

        mu_stds = []
        for i in range(num_labels):
            pos_mu, pos_std = self.fit(y_prob[trues == i, i])
            mu_stds.append([pos_mu, pos_std])

        return mu_stds
        





  

    
    
