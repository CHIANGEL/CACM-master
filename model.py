#!/usr/bin/env python
# -*- coding: utf-8 -*-
'''
@ref: A Context-Aware Click Model for Web Search
@author: Jia Chen, Jiaxin Mao, Yiqun Liu, Min Zhang, Shaoping Ma
@desc: Model training, testing, saving, and loading
'''
import os
import logging
import numpy as np
import torch
from torch.autograd import Variable
from tqdm import tqdm
from tensorboardX import SummaryWriter
from torch import nn
from CACMN import CACMN
from utils import *
import math

use_cuda = torch.cuda.is_available()

MINF = 1e-30

class Model(object):
    def __init__(self, args, query_size, doc_size, vtype_size):
        self.args = args
        self.logger = logging.getLogger("CACM")
        self.hidden_size = args.hidden_size
        self.optim_type = args.optim
        self.learning_rate = args.learning_rate
        self.weight_decay = args.weight_decay
        self.eval_freq = args.eval_freq
        self.global_step = args.load_model if args.load_model > -1 else 0
        self.patience = args.patience
        self.max_d_num = args.max_d_num
        self.use_knowledge = args.use_knowledge
        self.reg_relevance = args.reg_relevance
        if args.train:
            self.writer = SummaryWriter(self.args.summary_dir)

        self.model = CACMN(self.args, query_size, doc_size, vtype_size)

        if args.data_parallel:
            self.model = nn.DataParallel(self.model)
        if use_cuda:
            self.model = self.model.cuda()

        self.optimizer = self.create_train_op()
        self.criterion = nn.MSELoss()

    def compute_loss_rel(self, pred_rels, target_rels):  # compute loss for relevance
        total_loss = 0.0
        loss_list = []
        cnt = 0
        for batch_idx, rels in enumerate(target_rels):
            loss = 0.0
            cnt += 1
            last_click_pos = -1
            for position_idx, rel in enumerate(rels):
                if rel == 1:
                    last_click_pos = max(last_click_pos, position_idx)
            for position_idx, rel in enumerate(rels):  #
                if position_idx > last_click_pos:
                    break
                if rel == 0:
                    loss -= torch.log(1. - pred_rels[batch_idx][position_idx].view(1) + MINF)
                else:
                    loss -= torch.log(pred_rels[batch_idx][position_idx].view(1) + MINF)
            if loss != 0.0:
                loss_list.append(loss.data[0])
            total_loss += loss
        total_loss /= cnt
        return total_loss, loss_list

    def compute_loss(self, pred_scores, target_scores):  # compute loss for clicks
        total_loss = 0.0
        loss_list = []
        cnt = 0
        for batch_idx, scores in enumerate(target_scores):
            loss = 0.0
            cnt += 1
            for position_idx, score in enumerate(scores):  #
                if score == 0:
                    loss -= torch.log(1. - pred_scores[batch_idx][position_idx].view(1) + MINF)
                else:
                    loss -= torch.log(pred_scores[batch_idx][position_idx].view(1) + MINF)
            loss_list.append(loss.data[0])
            total_loss += loss
        total_loss /= cnt
        return total_loss, loss_list

    def compute_perplexity(self, pred_scores, target_scores):
        '''
        Compute the perplexity
        '''
        perplexity_at_rank = [0.0] * 10 # 10 docs per query
        total_num = len(pred_scores[0]) // 10
        for position_idx, score in enumerate(target_scores[0]):
            if score == 0:
                perplexity_at_rank[position_idx % 10] += torch.log2(1. - pred_scores[0][position_idx % 10].view(1) + MINF)
            else:
                perplexity_at_rank[position_idx % 10] += torch.log2(pred_scores[0][position_idx % 10].view(1) + MINF)
        return total_num, perplexity_at_rank

    def create_train_op(self):
        if self.optim_type == 'adagrad':
            optimizer = torch.optim.Adagrad(self.model.parameters(), lr=self.learning_rate, weight_decay=self.args.weight_decay)
        elif self.optim_type == 'adadelta':
            optimizer = torch.optim.Adadelta(self.model.parameters(), lr=self.learning_rate, weight_decay=self.args.weight_decay)
        elif self.optim_type == 'adam':
            optimizer = torch.optim.Adam(self.model.parameters(), lr=self.learning_rate, weight_decay=self.args.weight_decay)
        elif self.optim_type == 'rprop':
            optimizer = torch.optim.RMSprop(self.model.parameters(), lr=self.learning_rate, weight_decay=self.args.weight_decay)
        elif self.optim_type == 'sgd':
            optimizer = torch.optim.SGD(self.model.parameters(), lr=self.learning_rate, momentum=self.args.momentum,
                                        weight_decay=self.args.weight_decay)
        else:
            raise NotImplementedError('Unsupported optimizer: {}'.format(self.optim_type))
        return optimizer

    def adjust_learning_rate(self, decay_rate=0.5):
        for param_group in self.optimizer.param_groups:
            param_group['lr'] = param_group['lr'] * decay_rate

    def _train_epoch(self, train_batches, data, max_metric_value, metric_save, patience, step_pbar):
        evaluate = True
        exit_tag = False
        num_steps = self.args.num_steps
        check_point, batch_size = self.args.check_point, self.args.batch_size
        save_dir, save_prefix = self.args.model_dir, self.args.algo
        loss = 0.0

        for bitx, batch in enumerate(train_batches):
            knowledge_variable = Variable(torch.from_numpy(np.array(batch['knowledge_qs'], dtype=np.int64)))
            interaction_variable = Variable(torch.from_numpy(np.array(batch['interactions'], dtype=np.int64)))
            document_variable = Variable(torch.from_numpy(np.array(batch['doc_infos'], dtype=np.int64)))
            examination_context = Variable(torch.from_numpy(np.array(batch['exams'], dtype=np.int64)))
            target_clicks = batch['clicks']
            if use_cuda:
                knowledge_variable, interaction_variable, document_variable, examination_context = \
                    knowledge_variable.cuda(), interaction_variable.cuda(), document_variable.cuda(), examination_context.cuda()

            self.model.train()
            self.optimizer.zero_grad()
            relevances, exams, pred_clicks = self.model(knowledge_variable, interaction_variable, document_variable,
                                                        examination_context, data)
            loss_sum1, loss_list1 = self.compute_loss(pred_clicks, target_clicks)
            loss_sum2, loss_list2 = self.compute_loss_rel(relevances, target_clicks)
            loss += loss_sum1
            loss += loss_sum2 * self.reg_relevance

            if (bitx + 1) % 32 == 0:
                self.global_step += 1
                step_pbar.update(1)
                loss.backward()
                self.optimizer.step()
                self.writer.add_scalar('train/loss', loss.data[0], self.global_step)

                loss = 0.0

                if evaluate and self.global_step % self.eval_freq == 0:
                    if data.dev_set is not None or data.test_set is not None:
                        dev_batches = data.gen_mini_batches('dev', batch_size, shuffle=False)
                        dev_loss, dev_LL, dev_perplexity, dev_perplexity_at_rank = self.evaluate(dev_batches, data)
                        test_batches = data.gen_mini_batches('test', batch_size, shuffle=False)
                        test_loss, test_LL, test_perplexity, test_perplexity_at_rank = self.evaluate(test_batches, data)
                        self.writer.add_scalar("dev/loss", dev_loss, self.global_step)
                        self.writer.add_scalar("dev/log likelihood", dev_LL, self.global_step)
                        self.writer.add_scalar("dev/perplexity", dev_perplexity, self.global_step)
                        self.writer.add_scalar("test/loss", test_loss, self.global_step)
                        self.writer.add_scalar("test/log likelihood", test_LL, self.global_step)
                        self.writer.add_scalar("test/perplexity", test_perplexity, self.global_step)

                        label_batches = data.gen_mini_batches('label', batch_size, shuffle=False)
                        trunc_levels = [1, 3, 5, 10]
                        ndcgs_version1, ndcgs_version2 = self.ndcg(label_batches, data)
                        for trunc_level in trunc_levels:
                            ndcg_version1, ndcg_version2 = ndcgs_version1[trunc_level], ndcgs_version2[trunc_level]
                            self.writer.add_scalar("ndcg_version1/{}".format(trunc_level), ndcg_version1, self.global_step)
                            self.writer.add_scalar("ndcg_version2/{}".format(trunc_level), ndcg_version2, self.global_step)

                        if dev_loss < metric_save:
                            metric_save = dev_loss
                            patience = 0
                        else:
                            patience += 1
                        if patience >= self.patience:
                            self.adjust_learning_rate(self.args.lr_decay)
                            self.learning_rate *= self.args.lr_decay
                            self.writer.add_scalar('train/lr', self.learning_rate, self.global_step)
                            metric_save = dev_loss
                            patience = 0
                            self.patience += 1
                    else:
                        self.logger.warning('No dev/test set is loaded for evaluation in the dataset!')
                if check_point > 0 and self.global_step % check_point == 0:
                    self.save_model(save_dir, save_prefix)
                if self.global_step >= num_steps:
                    exit_tag = True

        return max_metric_value, exit_tag, metric_save, patience

    def train(self, data):
        max_metric_value, epoch, patience, metric_save = 0.0, 0, 0, 1e10
        step_pbar = tqdm(total=self.args.num_steps)
        exit_tag = False
        self.writer.add_scalar('train/lr', self.learning_rate, self.global_step)
        self.global_step += 1
        while not exit_tag:
            epoch += 1
            train_batches = data.gen_mini_batches('train', self.args.batch_size, shuffle=True)
            max_metric_value, exit_tag, metric_save, patience = self._train_epoch(train_batches, data, max_metric_value, metric_save,
                                                                                    patience, step_pbar)
    
    def ndcg(self, label_batches, data, result_dir=None, result_prefix=None, stop=-1):
        trunc_levels = [1, 3, 5, 10]
        ndcg_version1, ndcg_version2 = {}, {}
        useless_session, cnt_version1, cnt_version2 = {}, {}, {}
        for k in trunc_levels:
            ndcg_version1[k] = 0.0
            ndcg_version2[k] = 0.0
            useless_session[k] = 0
            cnt_version1[k] = 0
            cnt_version2[k] = 0
        with torch.no_grad():
            for b_itx, batch in enumerate(label_batches):
                print(b_itx)
                if b_itx == stop:
                    break
                if b_itx % 5000 == 0:
                    self.logger.info('Evaluation step {}.'.format(b_itx))
                knowledge_variable = Variable(torch.from_numpy(np.array(batch['knowledge_qs'], dtype=np.int64)))
                interaction_variable = Variable(torch.from_numpy(np.array(batch['interactions'], dtype=np.int64)))
                document_variable = Variable(torch.from_numpy(np.array(batch['doc_infos'], dtype=np.int64)))
                examination_context = Variable(torch.from_numpy(np.array(batch['exams'], dtype=np.int64)))
                true_relevances = batch['relevances'][0]
                if use_cuda:
                    knowledge_variable, interaction_variable, document_variable, examination_context = \
                        knowledge_variable.cuda(), interaction_variable.cuda(), document_variable.cuda(), \
                        examination_context.cuda()

                self.model.eval()
                relevances, exams, pred_clicks = self.model(knowledge_variable, interaction_variable, document_variable,
                                                            examination_context, data)
                relevances = relevances.data.cpu().numpy().reshape(-1).tolist()
                pred_rels = {}
                for idx, relevance in enumerate(relevances):
                    pred_rels[idx] = relevance
                
                #print('{}: \n{}'.format('relevances', relevances))
                #print('{}: \n{}'.format('true_relevances', true_relevances))
                #print('{}: \n{}'.format('pred_rels', pred_rels))
                for k in trunc_levels:
                    #print('\n{}: {}'.format('trunc_level', k))
                    ideal_ranking_relevances = sorted(true_relevances, reverse=True)[:k]
                    ranking = sorted([idx for idx in pred_rels], key = lambda idx : pred_rels[idx], reverse=True)
                    ranking_relevances = [true_relevances[idx] for idx in ranking[:k]]
                    #print('{}: {}'.format('ideal_ranking_relevances', ideal_ranking_relevances))
                    #print('{}: {}'.format('ranking', ranking))
                    #print('{}: {}'.format('ranking_relevances', ranking_relevances))
                    dcg = self.dcg(ranking_relevances)
                    idcg = self.dcg(ideal_ranking_relevances)
                    if dcg > idcg:
                        pprint.pprint(ranking_relevances)
                        pprint.pprint(ideal_ranking_relevances)
                        pprint.pprint(dcg)
                        pprint.pprint(idcg)
                        pprint.pprint(info_per_query)
                        assert 0
                    ndcg = dcg / idcg if idcg > 0 else 1.0
                    if idcg == 0:
                        useless_session[k] += 1
                        cnt_version2[k] += 1
                        ndcg_version2[k] += ndcg
                    else:
                        ndcg = dcg / idcg
                        cnt_version1[k] += 1
                        cnt_version2[k] += 1
                        ndcg_version1[k] += ndcg
                        ndcg_version2[k] += ndcg
                    #print('{}: {}'.format('dcg', dcg))
                    #print('{}: {}'.format('idcg', idcg))
                    #print('{}: {}'.format('ndcg', ndcg))
            '''for k in trunc_levels:
                print()
                print('{}: {}'.format('cnt_version1[{}]'.format(k), cnt_version1[k]))
                print('{}: {}'.format('useless_session[{}]'.format(k), useless_session[k]))
                print('{}: {}'.format('cnt_version2[{}]'.format(k), cnt_version2[k]))'''
            for k in trunc_levels:
                assert cnt_version1[k] + useless_session[k] == 2000
                assert cnt_version2[k] == 2000
                ndcg_version1[k] /= cnt_version1[k]
                ndcg_version2[k] /= cnt_version2[k]
        return ndcg_version1, ndcg_version2

    def dcg(self, ranking_relevances):
        """
        Computes the DCG for a given ranking_relevances
        """
        return sum([(2 ** relevance - 1) / math.log(rank + 2, 2) for rank, relevance in enumerate(ranking_relevances)])

    def evaluate(self, eval_batches, data, result_dir=None, result_prefix=None, stop=-1):
        eval_ouput = []
        total_loss, total_num = 0.0, 0
        log_likelihood = 0.0
        perplexity_num = 0
        perplexity_at_rank = [0.0] * 10 # 10 docs per query
        with torch.no_grad():
            for b_itx, batch in enumerate(eval_batches):
                if b_itx == stop:
                    break
                if b_itx % 5000 == 0:
                    self.logger.info('Evaluation step {}.'.format(b_itx))
                knowledge_variable = Variable(torch.from_numpy(np.array(batch['knowledge_qs'], dtype=np.int64)))
                interaction_variable = Variable(torch.from_numpy(np.array(batch['interactions'], dtype=np.int64)))
                document_variable = Variable(torch.from_numpy(np.array(batch['doc_infos'], dtype=np.int64)))
                examination_context = Variable(torch.from_numpy(np.array(batch['exams'], dtype=np.int64)))
                if use_cuda:
                    knowledge_variable, interaction_variable, document_variable, examination_context = \
                        knowledge_variable.cuda(), interaction_variable.cuda(), document_variable.cuda(), \
                        examination_context.cuda()

                self.model.eval()
                relevances, exams, pred_clicks = self.model(knowledge_variable, interaction_variable, document_variable,
                                                            examination_context, data)

                loss1, loss_list1 = self.compute_loss(pred_clicks, batch['clicks'])
                loss2, loss_list2 = self.compute_loss_rel(relevances, batch['clicks'])
                tmp_num, tmp_perplexity_at_rank = self.compute_perplexity(pred_clicks, batch['clicks'])
                perplexity_num += tmp_num
                perplexity_at_rank = [perplexity_at_rank[i] + tmp_perplexity_at_rank[i] for i in range(10)]
                log_likelihood -= loss1

                relevances = relevances.data.cpu().numpy()[0, :, 0].tolist()
                exams = exams.data.cpu().numpy()[0, :, 0].tolist()
                pred_clicks = pred_clicks.data.cpu().numpy()[0, :, 0].tolist()
                loss1 = loss1.data.cpu().numpy().tolist()[0]
                if loss2 != 0.0:
                    loss2 = loss2.data.cpu().numpy().tolist()[0]
                loss = loss1 + loss2 * self.reg_relevance
                eval_ouput.append([0, batch['clicks'][0], relevances, exams, pred_clicks, loss])
                total_loss += loss
                total_num += 1

            if result_dir is not None and result_prefix is not None:
                result_file = os.path.join(result_dir, result_prefix + '.txt')
                with open(result_file, 'w') as fout:
                    for sample in eval_ouput:
                        fout.write('\t'.join(map(str, sample)) + '\n')

                self.logger.info('Saving {} results to {}'.format(result_prefix, result_file))

            combine = self.args.combine
            if combine == 'exp_mul' or combine == 'exp_sigmoid_log':
                lamda = self.model.lamda.data.cpu()
                mu = self.model.mu.data.cpu()
                print('exp_mul:lambda=%s\tmu=%s' % (lamda, mu))
            elif combine == 'linear':
                alpha = self.model.alpha.data.cpu()
                beta = self.model.beta.data.cpu()
                print('linear:alpha=%s\tbeta=%s' % (alpha, beta))
            elif combine == 'nonlinear':
                w11 = self.model.w11.data.cpu()
                w12 = self.model.w12.data.cpu()
                w21 = self.model.w21.data.cpu()
                w22 = self.model.w22.data.cpu()
                w31 = self.model.w31.data.cpu()
                w32 = self.model.w32.data.cpu()
                print('nonlinear:w11=%s\tw12=%s\tw21=%s\tw22=%s\tw31=%s\tw32=%s' % (w11, w12, w21, w22, w31, w32))
                
            avg_span_loss = 1.0 * total_loss / total_num
            perplexity_at_rank = [2 ** (-x / perplexity_num) for x in perplexity_at_rank]
            perplexity = sum(perplexity_at_rank) / len(perplexity_at_rank)
            log_likelihood = log_likelihood / 10.0 / perplexity_num
        return avg_span_loss, log_likelihood, perplexity, perplexity_at_rank

    def generate_synthetic_dataset(self, batch_type, dataset, file_path, file_name, synthetic_type='deterministic', shuffle_split=None, amplification=1):
        assert batch_type in ['train', 'dev', 'test'], 'unsupported batch_type: {}'.format(batch_type)
        assert synthetic_type in ['deterministic', 'stochastic'], 'unsupported synthetic_type: {}'.format(synthetic_type)
        if synthetic_type == 'deterministic' and shuffle_split is None and amplification > 1:
            # assert amplification == 1, 'amplification should be 1 if using deterministic click generation and no 10-doc shuffles'
            # useless generative settings
            return 
        np.random.seed(2333)
        torch.manual_seed(2333)

        check_path(file_path)
        data_path = os.path.join(file_path, file_name)
        file = open(data_path, 'w')
        self.logger.info('Generating synthetic dataset based on the {} set...'.format(batch_type))
        self.logger.info('  - The synthetic dataset will be expended by {} times'.format(amplification))
        self.logger.info('  - Click generative type {}'.format(synthetic_type))
        self.logger.info('  - Shuffle split: {}'.format(shuffle_split if shuffle_split is not None else 'no shuffle on 10-doc list'))
        
        for amp_idx in range(amplification):
            self.logger.info('  - Generation at amplification {}'.format(amp_idx))
            eval_batches = dataset.gen_mini_batches(batch_type, self.args.batch_size, shuffle=False)

            for b_itx, batch in enumerate(eval_batches):
                #pprint.pprint(batch)
                if b_itx % 5000 == 0:
                    self.logger.info('    - Generating click sequence at step: {}.'.format(b_itx))

                # get the numpy version of input data
                knowledge_variable_numpy = np.array(batch['knowledge_qs'], dtype=np.int64)
                interaction_variable_numpy = np.array(batch['interactions'], dtype=np.int64)
                document_variable_numpy = np.array(batch['doc_infos'], dtype=np.int64)
                examination_context_numpy = np.array(batch['exams'], dtype=np.int64)
                clicks_numpy = np.array(batch['clicks'], dtype=np.int64)

                # print('{}: {}\n{}\n'.format('knowledge_variable_numpy', knowledge_variable_numpy.shape, knowledge_variable_numpy))
                # print('{}: {}\n{}\n'.format('document_variable_numpy', document_variable_numpy.shape, document_variable_numpy))
                # print('{}: {}\n{}\n'.format('interaction_variable_numpy', interaction_variable_numpy.shape, interaction_variable_numpy))
                # print('{}: {}\n{}\n'.format('examination_context_numpy', examination_context_numpy.shape, examination_context_numpy))
                # print('{}: {}\n{}\n'.format('clicks_numpy', clicks_numpy.shape, clicks_numpy))

                # shuffle uids and vids according to shuffle_split
                if shuffle_split is not None:
                    self.logger.info('    - Start shuffling uids & vids...')
                    assert type(shuffle_split) == type([0]), 'type of shuffle_split should be a list, but got {}'.format(type(shuffle_split))
                    assert len(shuffle_split) > 1, 'shuffle_split should have at least 2 elements but got only {}'.format(len(shuffle_split))
                    shuffle_split.sort()
                    assert shuffle_split[0] >= 1 and shuffle_split[-1] <= 11, 'all elements in shuffle_split should be in range of [1, 11], but got: {}'.format(shuffle_split)
                    query_num = knowledge_variable_numpy.shape[1] // 10
                    # add click infos to document_variable_numpy temporarily
                    document_variable_numpy[0, :, 3] = clicks_numpy.reshape(1, -1, 1)[0, :, 0]
                    #print('{}: {}\n{}\n'.format('document_variable_numpy', document_variable_numpy.shape, document_variable_numpy))
                    # only shuffle document_variable_numpy first
                    for i in range(query_num):
                        for split_idx in range(len(shuffle_split) - 1):
                            # NOTE: shuffle split list remain the same interface with NCM/GACMv1.0, so they need to minus one
                            split_left = shuffle_split[split_idx] - 1
                            split_right= shuffle_split[split_idx + 1] - 1
                            np.random.shuffle(document_variable_numpy[0, i*10+split_left:i*10+split_right, :])
                    # assign interaction_variable_numpy & examination_context_numpy according to shuffled document_variable_numpy
                    interaction_variable_numpy[0, 1:, :] = document_variable_numpy[0, :-1, :]
                    for i in range(query_num):
                        examination_context_numpy[0, i*10+1:i*10+10, :] = document_variable_numpy[0, i*10:i*10+9, :]
                        # remove click info and add iteration info back to document_variable_numpy
                        document_variable_numpy[0, i*10:i*10+10, 3].fill(i + 1)
                    # print('{}: {}\n{}\n'.format('document_variable_numpy', document_variable_numpy.shape, document_variable_numpy))
                    # print('{}: {}\n{}\n'.format('interaction_variable_numpy', interaction_variable_numpy.shape, interaction_variable_numpy))
                    # print('{}: {}\n{}\n'.format('examination_context_numpy', examination_context_numpy.shape, examination_context_numpy))
                #exit(0)

                # get the tensor version of input data (maybe shuffled) from the numpy version
                knowledge_variable = Variable(torch.from_numpy(knowledge_variable_numpy))
                interaction_variable = Variable(torch.from_numpy(interaction_variable_numpy))
                document_variable = Variable(torch.from_numpy(document_variable_numpy))
                examination_context = Variable(torch.from_numpy(examination_context_numpy))
                if use_cuda:
                    knowledge_variable, interaction_variable = knowledge_variable.cuda(), interaction_variable.cuda()
                    document_variable, examination_context = document_variable.cuda(), examination_context.cuda()
                # print('{}: {}\n{}\n'.format('document_variable_numpy', document_variable_numpy.shape, document_variable_numpy))
                # print('{}: {}\n{}\n'.format('interaction_variable_numpy', interaction_variable_numpy.shape, interaction_variable_numpy))
                # print('{}: {}\n{}\n'.format('examination_context_numpy', examination_context_numpy.shape, examination_context_numpy))
                # print('{}: {}\n{}\n'.format('knowledge_variable', knowledge_variable.shape, knowledge_variable))
                # print('{}: {}\n{}\n'.format('interaction_variable', interaction_variable[:, :, 1].shape, interaction_variable[:, :, 1]))
                # print('{}: {}\n{}\n'.format('document_variable', document_variable.shape, document_variable))
                # print('{}: {}\n{}\n'.format('examination_context', examination_context.shape, examination_context))
                # exit(0)

                # start predict the click info
                self.model.eval()
                click_list = []
                doc_num = knowledge_variable.shape[1]
                query_num = doc_num // 10
                for idx in range(doc_num):
                    knowledge_input = knowledge_variable[0, :idx+1, :].view(1, idx+1, 10)
                    interaction_input = interaction_variable[0, :idx+1, :].view(1, idx+1, 4)
                    document_input = document_variable[0, :idx+1, :].view(1, idx+1, 4)
                    examination_input = examination_context[0, :idx+1, :].view(1, idx+1, 4)
                    # print('{}: {}\n{}\n'.format('interaction_input', interaction_input.shape, interaction_input))
                    # print('{}: {}\n{}\n'.format('examination_input', examination_input.shape, examination_input))
                    relevances, exams, pred_clicks = self.model(knowledge_input, interaction_input, document_input, examination_input, dataset)
                    if synthetic_type == 'deterministic':
                        CLICK_ = (pred_clicks[0, -1, 0] > 0.5).type(knowledge_variable.dtype)
                        click_list.append(CLICK_)
                        # print('{}: {}\n{}'.format('pred_clicks', pred_clicks.shape, pred_clicks))
                        # print('{}: {}\n{}'.format('CLICK_', CLICK_.shape, CLICK_))
                        # print('{}: {}\n{}'.format('click_list', len(click_list), click_list))
                        # print()
                    elif synthetic_type == 'stochastic':
                        random_tmp = torch.rand(pred_clicks[0, -1, 0].shape)
                        if use_cuda:
                            random_tmp = random_tmp.cuda()
                        CLICK_ = (random_tmp <= pred_clicks[0, -1, 0]).type(knowledge_variable.dtype)
                        click_list.append(CLICK_)
                        # print('{}: {}\n{}'.format('pred_clicks', pred_clicks.shape, pred_clicks))
                        # print('{}: {}\n{}'.format('random_tmp', random_tmp.shape, random_tmp))
                        # print('{}: {}\n{}'.format('CLICK_', CLICK_.shape, CLICK_))
                        # print('{}: {}\n{}'.format('click_list', len(click_list), click_list))
                        # print()
                    if idx < doc_num - 1:
                        interaction_variable[0, idx+1:idx+2, 3] = CLICK_
                        if idx % 10 != 9:
                            examination_context[0, idx+1:idx+2, 3] = CLICK_
                    # print('{}: {}\n{}\n'.format('relevances', relevances.shape, relevances))
                    # print('{}: {}\n{}\n'.format('exams', exams.shape, exams))
                    # print('{}: {}\n{}\n'.format('pred_clicks', pred_clicks.shape, pred_clicks))
                CLICKS_ = torch.stack(click_list, dim=0).view(-1, 10).cpu().numpy().tolist()
                UIDS = document_variable[0, :, 0].view(-1, 10).cpu().numpy().tolist()
                VIDS = document_variable[0, :, 2].view(-1, 10).cpu().numpy().tolist()
                # print(CLICKS_)
                # print(UIDS)
                # print(VIDS)
                for i in range(query_num):
                    qid = int(knowledge_variable[0, i*10, i])
                    uids = UIDS[i]
                    vids = VIDS[i]
                    clicks = CLICKS_[i]
                    # print(qid)
                    # print(uids)
                    # print(vids)
                    # print(clicks)
                    # print()
                    file.write("{}\t{}\t{}\t{}\t{}\t{}\t{}\n".format(0, qid, 0, 0, str(uids), str(vids), str(clicks)))
                # exit(0)
        self.logger.info('Finish synthetic dataset generation...')
        file.close()

    def save_model(self, model_dir, model_prefix):
        torch.save(self.model.state_dict(), os.path.join(model_dir, model_prefix+'_{}.model'.format(self.global_step)))
        torch.save(self.optimizer.state_dict(), os.path.join(model_dir, model_prefix + '_{}.optimizer'.format(self.global_step)))
        self.logger.info('Model and optimizer saved in {}, with prefix {} and global step {}.'.format(model_dir,
                                                                                                        model_prefix,
                                                                                                        self.global_step))

    def load_model(self, model_dir, model_prefix, global_step):
        optimizer_path = os.path.join(model_dir, model_prefix + '_{}.optimizer'.format(global_step))
        if not os.path.isfile(optimizer_path):
            optimizer_path = os.path.join(model_dir, model_prefix + '_best_{}.optimizer'.format(global_step))
        if os.path.isfile(optimizer_path):
            self.optimizer.load_state_dict(torch.load(optimizer_path))
            self.logger.info('Optimizer restored from {}, with prefix {} and global step {}.'.format(model_dir,
                                                                                                        model_prefix,
                                                                                                        global_step))
        model_path = os.path.join(model_dir, model_prefix + '_{}.model'.format(global_step))
        if not os.path.isfile(model_path):
            model_path = os.path.join(model_dir, model_prefix + '_best_{}.model'.format(global_step))
        if use_cuda:
            state_dict = torch.load(model_path)
        else:
            state_dict = torch.load(model_path, map_location=lambda storage, loc: storage)
        self.model.load_state_dict(state_dict)
        self.logger.info('Model restored from {}, with prefix {} and global step {}.'.format(model_dir, model_prefix, global_step))
