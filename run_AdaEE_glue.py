from __future__ import absolute_import, division, print_function

import argparse
import glob
import logging
import os
import random
import numpy as np
import torch
from torch.utils.data import (DataLoader, RandomSampler, SequentialSampler,
                              TensorDataset)
from torch.utils.data.distributed import DistributedSampler
import torch.nn.functional as F

try:
    from torch.utils.tensorboard import SummaryWriter
except:
    from tensorboardX import SummaryWriter

from tqdm import tqdm

from transformers import (WEIGHTS_NAME, BertConfig,
                          BertTokenizer,
                          RobertaConfig,
                          )
from model.tokenization_roberta import RobertaTokenizer
from model.modeling_adaEE_bert import BertForSequenceClassification
from model.modeling_adaEE_roberta import RobertaForSequenceClassification

from transformers import AdamW, get_linear_schedule_with_warmup

from model.Glue_compute_metrics import glue_compute_metrics as compute_metrics

# from transformers import glue_convert_examples_to_features as convert_examples_to_features
from model.Glue import glue_convert_examples_to_features as convert_examples_to_features
from model.Glue import glue_processors as processors
from model.Glue import glue_output_modes as output_modes
logger = logging.getLogger(__name__)



MODEL_CLASSES = {
    'bert': (BertConfig, BertForSequenceClassification, BertTokenizer),
    'roberta': (RobertaConfig, RobertaForSequenceClassification, RobertaTokenizer),
}


def set_seed(args):
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    if args.n_gpu > 0:
        torch.cuda.manual_seed_all(args.seed)


def get_wanted_result(result):
    if "spearmanr" in result:
        print_result = result["spearmanr"]
    elif "f1" in result:
        print_result = result["f1"]
    elif "mcc" in result:
        print_result = result["mcc"]
    elif "acc" in result:
        print_result = result["acc"]
    else:
        print(result)
        exit(1)
    return print_result

def compute_agr_mask(domain_grads):
    """ Agreement mask. """
    grad_sign = torch.stack([torch.sign(g) for g in domain_grads])
    # True if all componentes agree, False if not
    agr_mask = torch.where(grad_sign.sum(0).abs() == len(domain_grads), 1, 0)
    return agr_mask.bool()

def train(args, train_dataset, model, tokenizer,  kd_loss="kd"):
    """ Train the model """
    if args.local_rank in [-1, 0]:
        tb_writer = SummaryWriter()

    args.train_batch_size = args.per_gpu_train_batch_size * max(1, args.n_gpu)
    train_sampler = RandomSampler(train_dataset) if args.local_rank == -1 else DistributedSampler(train_dataset)
    train_dataloader = DataLoader(train_dataset, sampler=train_sampler, batch_size=args.train_batch_size)

    if args.max_steps > 0:
        t_total = args.max_steps
        args.num_train_epochs = args.max_steps // (len(train_dataloader) // args.gradient_accumulation_steps) + 1
    else:
        t_total = len(train_dataloader) // args.gradient_accumulation_steps * args.num_train_epochs

    # Prepare optimizer and schedule (linear warmup and decay)
    no_decay = ['bias', 'LayerNorm.weight']
    optimizer_grouped_parameters = [
        {'params': [p for n, p in model.named_parameters() if not any(nd in n for nd in no_decay)],
         'weight_decay': args.weight_decay},
        {'params': [p for n, p in model.named_parameters() if any(nd in n for nd in no_decay)],
         'weight_decay': 0.0}
        ]

    optimizer = AdamW(optimizer_grouped_parameters, lr=args.learning_rate, eps=args.adam_epsilon)
    scheduler = get_linear_schedule_with_warmup(optimizer, num_warmup_steps=args.warmup_steps, num_training_steps=t_total)

    if args.fp16:
        try:
            from apex import amp
        except ImportError:
            raise ImportError("Please install apex from https://www.github.com/nvidia/apex to use fp16 training.")
        model, optimizer = amp.initialize(model, optimizer, opt_level=args.fp16_opt_level)

    # multi-gpu training (should be after apex fp16 initialization)
    if args.n_gpu > 1:
        model = torch.nn.DataParallel(model)

    # Distributed training (should be after apex fp16 initialization)
    if args.local_rank != -1:
        model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[args.local_rank],
                                                          output_device=args.local_rank,
                                                          find_unused_parameters=True)
    # Train!
    logger.info("***** Running training *****")
    logger.info("  Num examples = %d", len(train_dataset))
    logger.info("  Num Epochs = %d", args.num_train_epochs)
    logger.info("  Instantaneous batch size per GPU = %d", args.per_gpu_train_batch_size)
    logger.info("  Total train batch size (w. parallel, distributed & accumulation) = %d",
                args.train_batch_size * args.gradient_accumulation_steps * (torch.distributed.get_world_size() if args.local_rank != -1 else 1))
    logger.info("  Gradient Accumulation steps = %d", args.gradient_accumulation_steps)
    logger.info("  Total optimization steps = %d", t_total)

    global_step = 0
    tr_loss, logging_loss = 0.0, 0.0

    tr_kld_loss, logging_kld_loss = 0.0, 0.0
    tr_ce_loss, logging_ce_loss = 0.0, 0.0

    num_aangle = 0
    model.zero_grad()
    set_seed(args)
    for i in range(int(args.num_train_epochs)):
        epoch_iterator = tqdm(train_dataloader, desc="Iteration", disable=args.local_rank not in [-1, 0])
        for step, batch in enumerate(epoch_iterator):
            model.train()
            batch = tuple(t.to(args.device) for t in batch)
            inputs = {'input_ids': batch[0],
                      'attention_mask': batch[1],
                      'labels': batch[3]}
            if args.model_type != 'distilbert':
                inputs['token_type_ids'] = batch[2] if args.model_type in ['bert', 'xlnet'] else None  # XLM, DistilBERT and RoBERTa don't use segment_ids
            inputs['kd_loss_type'] =  kd_loss
            outputs = model(**inputs)
            loss = outputs[0]

            if type(loss) == tuple:
                ce_loss = loss[2]
                kld_loss = loss[1]
                full_loss = loss[0]
                l_loss = loss[3]

            if args.n_gpu > 1:
                ce_loss = ce_loss.mean()
                kld_loss = kld_loss.mean()
                full_loss = full_loss.mean()
                l_loss = l_loss.mean()

            if args.gradient_accumulation_steps > 1:
                ce_loss = ce_loss / args.gradient_accumulation_steps
                kld_loss = kld_loss / args.gradient_accumulation_steps
                full_loss = full_loss / args.gradient_accumulation_steps
                l_loss = l_loss / args.gradient_accumulation_steps
            if args.gd:
                if args.fp16:
                    with amp.scale_loss(kld_loss, optimizer) as scaled_loss:
                        scaled_loss.backward(retain_graph=True)
                else:
                    kld_loss.backward(retain_graph=True)

                var_list_kld = []
                grad_list_kld = []
                grad_size_kld = []
                for name, param in model.named_parameters():
                    if 'encoder' in name and 'LayerNorm' not in name and 'bias' not in name and param.grad is not None:
                        var_list_kld.append(name)
                        grad_list_kld.append(param.grad.view(-1))
                        grad_size_kld.append(param.grad.shape)
                grad_list_kld = torch.cat(grad_list_kld)

                if args.fp16:
                    with amp.scale_loss(ce_loss, optimizer) as scaled_loss:
                        scaled_loss.backward(retain_graph=True)
                else:
                    ce_loss.backward()

                var_list_ce = []
                grad_list_ce = []
                grad_size_ce = []
                for name, param in model.named_parameters():
                    if 'encoder' in name and 'LayerNorm' not in name and 'bias' not in name and param.grad is not None:
                        var_list_ce.append(name)
                        grad_list_ce.append(param.grad.view(-1))
                        grad_size_ce.append(param.grad.shape)
                grad_list_ce = torch.cat(grad_list_ce)
                assert var_list_kld == var_list_ce

                cos_v =  F.cosine_similarity(grad_list_kld.unsqueeze(0), grad_list_ce.unsqueeze(0))
                l2norm_last = torch.norm(grad_list_kld)
                l2norm_multi = torch.norm(grad_list_ce)

                inner_product = torch.sum(grad_list_ce * grad_list_kld)
                proj_direction = inner_product / torch.sum(grad_list_ce * grad_list_ce)
                grad_list_kld = grad_list_kld - torch.min(proj_direction, torch.zeros([1]).cuda()) * grad_list_ce
                start_idx = 0
                idx = 0
                for name, param in model.named_parameters():
                    if name in var_list_ce:
                        grad_shape = grad_size_ce[idx]
                        flatten_dim = np.prod([grad_shape[i] for i in range(len(grad_shape))])
                        proj_grad_last = torch.reshape(grad_list_kld[start_idx:start_idx+flatten_dim], grad_shape)
                        proj_grad_multi = torch.reshape(grad_list_ce[start_idx:start_idx+flatten_dim], grad_shape)
                        param.grad = proj_grad_last.detach() + proj_grad_multi.detach()
                        start_idx += flatten_dim
                        idx+= 1
            else:
                if args.fp16:
                    with amp.scale_loss(loss, optimizer) as scaled_loss:
                        scaled_loss.backward()
                else:
                    full_loss.backward()

            tr_loss += full_loss.item()
            tr_ce_loss += ce_loss.item()
            tr_kld_loss += kld_loss.item()

            if (step + 1) % args.gradient_accumulation_steps == 0:
                if args.fp16:
                    torch.nn.utils.clip_grad_norm_(amp.master_params(optimizer), args.max_grad_norm)
                else:
                    torch.nn.utils.clip_grad_norm_(model.parameters(), args.max_grad_norm)

                optimizer.step()
                scheduler.step()  # Update learning rate schedule
                model.zero_grad()
                global_step += 1

                if args.local_rank in [-1, 0] and args.logging_steps > 0 and global_step % args.logging_steps == 0:
                    # Log metrics
                    if args.local_rank == -1 and args.evaluate_during_training:  # Only evaluate when single GPU otherwise metrics may not average well
                        results = evaluate(args, model, tokenizer,eval_threshold=args.eval_threshold)
                        print("results",results)
                        for key, value in results.items():
                            tb_writer.add_scalar('eval_{}'.format(key), value, global_step)
                    tb_writer.add_scalar('lr', scheduler.get_lr()[0], global_step)
                    tb_writer.add_scalar('full_loss', (tr_loss - logging_loss)/args.logging_steps, global_step)
                    tb_writer.add_scalar('kld_loss', (tr_kld_loss - logging_kld_loss)/args.logging_steps, global_step)
                    tb_writer.add_scalar('ce_loss', (tr_ce_loss - logging_ce_loss)/args.logging_steps, global_step)
                    logging_loss = tr_loss
                    logging_kld_loss = tr_kld_loss
                    logging_ce_loss = tr_ce_loss


                if args.local_rank in [-1, 0] and args.save_steps > 0 and global_step % args.save_steps == 0:
                    # Save model checkpoint
                    output_dir = os.path.join(args.output_dir, 'checkpoint-{}'.format(global_step))
                    if not os.path.exists(output_dir):
                        os.makedirs(output_dir)
                    model_to_save = model.module if hasattr(model,'module') else model  # Take care of distributed/parallel training
                    model_to_save.save_pretrained(output_dir)
                    # tokenizer.save_pretrained(output_dir)
                    torch.save(args, os.path.join(output_dir, 'training_args.bin'))
                    logger.info("Saving model checkpoint to %s", output_dir)

            if args.max_steps > 0 and global_step > args.max_steps:
                epoch_iterator.close()
                break

    if args.local_rank in [-1, 0]:
        tb_writer.close()

    return global_step, tr_loss / global_step


def evaluate(args, model, tokenizer, prefix="", output_layer=-1, eval_threshold = False):
    # Loop to handle MNLI double evaluation (matched, mis-matched)
    eval_task_names = ("mnli", "mnli-mm") if args.task_name == "mnli" else (args.task_name,)
    eval_outputs_dirs = (args.output_dir, args.output_dir + '-MM') if args.task_name == "mnli" else (args.output_dir,)
    results = {}
    for eval_task, eval_output_dir in zip(eval_task_names, eval_outputs_dirs):

        eval_dataset = load_and_cache_examples(args, eval_task, tokenizer, evaluate=True)

        if not os.path.exists(eval_output_dir) and args.local_rank in [-1, 0]:
            os.makedirs(eval_output_dir)

        args.eval_batch_size = args.per_gpu_eval_batch_size * max(1, args.n_gpu)
        # Note that DistributedSampler samples randomly
        eval_sampler = SequentialSampler(eval_dataset) if args.local_rank == -1 else DistributedSampler(eval_dataset)
        eval_dataloader = DataLoader(eval_dataset, sampler=eval_sampler, batch_size=args.eval_batch_size)

        # multi-gpu eval
        if args.n_gpu > 1:
            model = torch.nn.DataParallel(model)

        # Eval!
        logger.info("***** Running evaluation {} *****".format(prefix))
        logger.info("  Num examples = %d", len(eval_dataset))
        logger.info("  Batch size = %d", args.eval_batch_size)
        eval_loss = 0.0
        nb_eval_steps = 0
        preds = None
        out_label_ids = None
        exit_layer_counter = {(i + 1): 0 for i in range(model.num_layers)}

        for batch in tqdm(eval_dataloader, desc="Evaluating"):
            model.eval()
            batch = tuple(t.to(args.device) for t in batch)

            with torch.no_grad():
                inputs = {'input_ids': batch[0],
                          'attention_mask': batch[1],
                          'labels': batch[3]}
                if args.model_type != 'distilbert':
                    inputs['token_type_ids'] = batch[2] if args.model_type in ['bert','xlnet'] else None  # XLM, DistilBERT and RoBERTa don't use segment_ids
                if output_layer >= 0:
                    inputs['output_layer'] = output_layer
                outputs = model(**inputs)
                if eval_threshold:
                    exit_layer_counter[outputs[-1]] += 1
                tmp_eval_loss, logits = outputs[:2]
                if type(tmp_eval_loss) == tuple:
                    tmp_eval_loss = tmp_eval_loss[0]
                eval_loss += tmp_eval_loss.mean().item()
            nb_eval_steps += 1
            if preds is None:
                preds = logits.detach().cpu().numpy()
                out_label_ids = inputs['labels'].detach().cpu().numpy()
            else:
                preds = np.append(preds, logits.detach().cpu().numpy(), axis=0)
                out_label_ids = np.append(out_label_ids, inputs['labels'].detach().cpu().numpy(), axis=0)
        eval_loss = eval_loss / nb_eval_steps
        print("eval_loss", eval_loss)
        if args.output_mode == "classification":
            preds = np.argmax(preds, axis=1)
        elif args.output_mode == "regression":
            preds = np.squeeze(preds)
        result = compute_metrics(eval_task, preds, out_label_ids)
        print("result",result)
        results.update(result)

        if eval_threshold:
            print("Exit layer counter", exit_layer_counter)
            actual_cost = sum([l * c for l, c in exit_layer_counter.items()])
            full_cost = len(eval_dataloader) * model.num_layers
            print("Expected saving", actual_cost / full_cost)
            if args.early_exit_entropy >= 0:
                save_fname = args.plot_data_dir + '/' + \
                             args.model_name_or_path + \
                             "/entropy_{}.npy".format(args.early_exit_entropy)
                if not os.path.exists(os.path.dirname(save_fname)):
                    os.makedirs(os.path.dirname(save_fname))
                print_result = get_wanted_result(result)
                print("print_result", print_result,eval_task)
                np.save(save_fname,
                        np.array([exit_layer_counter,
                                  actual_cost / full_cost,
                                  print_result]))
        output_eval_file = os.path.join(eval_output_dir+"/"+prefix, "eval_results_{}.txt".format(args.early_exit_entropy))
        if not os.path.exists(eval_output_dir+"/"+prefix):
            os.makedirs(eval_output_dir+"/"+prefix)
        Expectedsaving = actual_cost / full_cost
        with open(output_eval_file, "w") as writer:
            logger.info("***** Eval results {} *****".format(prefix))
            for key in sorted(result.keys()):
                logger.info("  %s = %s", key, str(result[key]))
                writer.write("%s = %s\n" % (key, str(result[key])))
                writer.write("Expected saving = %s\n" % Expectedsaving)
                writer.write("Exit layer counter = %s"% exit_layer_counter)

    return results


def load_and_cache_examples(args, task, tokenizer, evaluate=False):
    if args.local_rank not in [-1, 0] and not evaluate:
        torch.distributed.barrier()  # Make sure only the first process in distributed training process the dataset, and the others will use the cache

    processor = processors[task]()
    output_mode = output_modes[task]
    # Load data features from cache or dataset file
    cached_features_file = os.path.join(args.data_dir, 'cached_{}_{}_{}_{}'.format(
        'dev' if evaluate else 'train',
        list(filter(None, args.model_name_or_path.split('/'))).pop(),
        str(args.max_seq_length),
        str(task)))

    if os.path.exists(cached_features_file) and not args.overwrite_cache:
        logger.info("Loading features from cached file %s", cached_features_file)
        features = torch.load(cached_features_file)
    else:
        logger.info("Creating features from dataset file at %s", args.data_dir)
        label_list = processor.get_labels()
        if task in ['mnli', 'mnli-mm'] and args.model_type in ['roberta']:
            # HACK(label indices are swapped in RoBERTa pretrained model)
            label_list[1], label_list[2] = label_list[2], label_list[1]
        examples = processor.get_dev_examples(args.data_dir) if evaluate else processor.get_train_examples(args.data_dir)
        features = convert_examples_to_features(examples,
                                                tokenizer,
                                                label_list=label_list,
                                                max_length=args.max_seq_length,
                                                output_mode=output_mode,
                                                )
        if args.local_rank in [-1, 0]:
            logger.info("Saving features into cached file %s", cached_features_file)
            torch.save(features, cached_features_file)

    if args.local_rank == 0 and not evaluate:
        torch.distributed.barrier()  # Make sure only the first process in distributed training process the dataset, and the others will use the cache

    # Convert to Tensors and build dataset
    all_input_ids = torch.tensor([f.input_ids for f in features], dtype=torch.long)
    all_attention_mask = torch.tensor([f.attention_mask for f in features], dtype=torch.long)
    all_token_type_ids = torch.tensor([f.token_type_ids for f in features], dtype=torch.long)
    if output_mode == "classification":
        all_labels = torch.tensor([f.label for f in features], dtype=torch.long)
    elif output_mode == "regression":
        all_labels = torch.tensor([f.label for f in features], dtype=torch.float)

    dataset = TensorDataset(all_input_ids, all_attention_mask, all_token_type_ids, all_labels)
    return dataset


def main():
    parser = argparse.ArgumentParser()

    #  Required parameters

    parser.add_argument("--data_dir", default=None, type=str, required=True,
                        help="The input data dir. Should contain the .tsv files (or other data files) for the task.")
    parser.add_argument("--model_type", default=None, type=str, required=True,
                        help="Model type selected in the list: " + ", ".join(MODEL_CLASSES.keys()))
    parser.add_argument("--model_name_or_path", default=None, type=str, required=True,
                        help="Path to pre-trained model or shortcut name  " )
    parser.add_argument("--task_name", default=None, type=str, required=True,
                        help="The name of the task to train selected in the list: " + ", ".join(processors.keys()))
    parser.add_argument("--output_dir", default=None, type=str, required=True,
                        help="The output directory where the model predictions and checkpoints will be written.")
    parser.add_argument("--plot_data_dir", default="./plotting/", type=str, required=False,
                        help="The directory to store data for plotting figures.")

    #  Other parameters
    parser.add_argument("--config_name", default="", type=str,
                        help="Pretrained config name or path if not the same as model_name")
    parser.add_argument("--tokenizer_name", default="", type=str,
                        help="Pretrained tokenizer name or path if not the same as model_name")
    parser.add_argument("--cache_dir", default="", type=str,
                        help="Where do you want to store the pre-trained models downloaded from s3")
    parser.add_argument("--max_seq_length", default=128, type=int, help="The maximum total input sequence length after tokenization. Sequences longer " "than this will be truncated, sequences shorter will be padded.")
    parser.add_argument("--do_train", action='store_true', help="Whether to run training.")
    parser.add_argument("--do_eval", action='store_true', help="Whether to run eval on the dev set.")
    parser.add_argument("--evaluate_during_training", action='store_true', help= "Run evaluation during training at each logging step.")
    parser.add_argument("--do_lower_case", action='store_true', help="Set this flag if you are using an uncased model.")
    parser.add_argument("--eval_each_layer", action='store_true', help="Set this flag to evaluate each layer.")
    parser.add_argument("--eval_threshold", action='store_true',help="Set this flag if it's evaluating threshold models")

    parser.add_argument("--per_gpu_train_batch_size", default=8, type=int, help="Batch size per GPU/CPU for training.")
    parser.add_argument("--per_gpu_eval_batch_size", default=1, type=int,  help="Batch size per GPU/CPU for evaluation.")
    parser.add_argument('--gradient_accumulation_steps', type=int, default=1, help= "Number of updates steps to accumulate before performing a backward/update pass.")
    parser.add_argument("--learning_rate", default=5e-5, type=float, help="The initial learning rate for Adam.")
    parser.add_argument("--weight_decay", default=0.0, type=float, help="Weight deay if we apply some.")
    parser.add_argument("--adam_epsilon", default=1e-8, type=float, help="Epsilon for Adam optimizer.")
    parser.add_argument("--max_grad_norm", default=1.0, type=float, help="Max gradient norm.")
    parser.add_argument("--num_train_epochs", default=3.0, type=float,help="Total number of training epochs to perform.")
    parser.add_argument("--max_steps", default=-1, type=int, help="If > 0: set total number of training steps to perform. Override num_train_epochs.")
    parser.add_argument("--warmup_steps", default=0, type=int, help="Linear warmup over warmup_steps.")
    parser.add_argument("--early_exit_entropy", default=-1, type=float, help="Entropy threshold for early exit.")

    parser.add_argument('--logging_steps', type=int, default=50, help="Log every X updates steps.")
    parser.add_argument('--save_steps', type=int, default=50, help="Save checkpoint every X updates steps.")
    parser.add_argument("--eval_all_checkpoints", action='store_true', help="Evaluate all checkpoints starting with the same prefix as model_name ending and ending with step number")
    parser.add_argument("--no_cuda", action='store_true', help="Avoid using CUDA when available")
    parser.add_argument('--overwrite_output_dir', action='store_true', help="Overwrite the content of the output directory")
    parser.add_argument('--overwrite_cache', action='store_true',  help="Overwrite the cached training and evaluation sets")
    parser.add_argument('--seed', type=int, default=42, help="random seed for initialization")

    parser.add_argument('--fp16', action='store_true', help="Whether to use 16-bit (mixed) precision (through NVIDIA apex) instead of 32-bit")
    parser.add_argument('--fp16_opt_level', type=str, default='O1', help="For fp16: Apex AMP optimization level selected in ['O0', 'O1', 'O2', and 'O3']."
                             "See details at https://nvidia.github.io/apex/amp.html")
    parser.add_argument("--local_rank", type=int, default=-1, help="For distributed training: local_rank")
    parser.add_argument('--gamma', type=float, default=0.9, help='gamma for kld loss')
    parser.add_argument('--temper', type=float, default=3.0, help='Temperature for KD')
    parser.add_argument('--kd_loss', type=str, default='kd',help='distillation loss')
    parser.add_argument('--gd', action='store_true', help="Whether to use gd")

    args = parser.parse_args()

    if os.path.exists(args.output_dir) and os.listdir( args.output_dir) and args.do_train and not args.overwrite_output_dir:
        raise ValueError(
            "Output directory ({}) already exists and is not empty. Use --overwrite_output_dir to overcome.".format( args.output_dir))


    # Setup CUDA, GPU & distributed training

    if args.local_rank == -1 or args.no_cuda:
        device = torch.device("cuda" if torch.cuda.is_available() and not args.no_cuda else "cpu")
        args.n_gpu = torch.cuda.device_count()
    else:  # Initializes the distributed backend which will take care of sychronizing nodes/GPUs
        torch.cuda.set_device(args.local_rank)
        device = torch.device("cuda", args.local_rank)
        torch.distributed.init_process_group(backend='nccl')
        args.n_gpu = 1
    args.device = device

    # Setup logging
    logging.basicConfig(format='%(asctime)s - %(levelname)s - %(name)s -   %(message)s',
                        datefmt='%m/%d/%Y %H:%M:%S',
                        level=logging.INFO if args.local_rank in [-1, 0] else logging.WARN)
    logger.warning("Process rank: %s, device: %s, n_gpu: %s, distributed training: %s, 16-bits training: %s",
                   args.local_rank, device, args.n_gpu, bool(args.local_rank != -1), args.fp16)

    # Set seed
    set_seed(args)

    # Prepare GLUE task
    args.task_name = args.task_name.lower()
    if args.task_name not in processors:
        raise ValueError("Task not found: %s" % (args.task_name))
    processor = processors[args.task_name]()
    args.output_mode = output_modes[args.task_name]
    label_list = processor.get_labels()
    num_labels = len(label_list)

    # Load pretrained model and tokenizer
    if args.local_rank not in [-1, 0]:
        torch.distributed.barrier()  # Make sure only the first process in distributed training will download model & vocab

    args.model_type = args.model_type.lower()
    config_class, model_class, tokenizer_class = MODEL_CLASSES[args.model_type]
    config = config_class.from_pretrained(args.config_name if args.config_name else args.model_name_or_path, num_labels=num_labels,
                                          finetuning_task=args.task_name, cache_dir=args.cache_dir if args.cache_dir else None)
    tokenizer = tokenizer_class.from_pretrained(args.tokenizer_name if args.tokenizer_name else args.model_name_or_path,
                                                do_lower_case=args.do_lower_case, cache_dir=args.cache_dir if args.cache_dir else None)
    model = model_class.from_pretrained(args.model_name_or_path, from_tf=bool('.ckpt' in args.model_name_or_path),
                                        config=config, cache_dir=args.cache_dir if args.cache_dir else None)

    if args.model_type == "bert":
        model.bert.encoder.set_early_exit_entropy(args.early_exit_entropy)
        model.bert.init_early_exit_pooler()
    else:
        model.roberta.encoder.set_early_exit_entropy(args.early_exit_entropy)
        model.roberta.init_early_exit_pooler()

    if args.local_rank == 0:
        torch.distributed.barrier()  # Make sure only the first process in distributed training will download model & vocab

    model.to(args.device)

    logger.info("Training/evaluation parameters %s", args)
    # Training
    if args.do_train:
        train_dataset = load_and_cache_examples(args, args.task_name, tokenizer, evaluate=False)
        global_step, tr_loss = train(args, train_dataset, model, tokenizer, kd_loss=args.kd_loss)
        logger.info(" global_step = %s, average loss = %s", global_step, tr_loss)


    # Saving best-practices: if you use defaults names for the model, you can reload it using from_pretrained()
    if args.do_train and (args.local_rank == -1 or torch.distributed.get_rank() == 0):
        # Create output directory if needed
        if not os.path.exists(args.output_dir) and args.local_rank in [-1, 0]:
            os.makedirs(args.output_dir)

        logger.info("Saving model checkpoint to %s", args.output_dir)
        # Save a trained model, configuration and tokenizer using `save_pretrained()`
        # They can then be reloaded using `from_pretrained()`
        model_to_save = model.module if hasattr(model,'module') else model  # Take care of distributed/parallel training
        model_to_save.save_pretrained(args.output_dir)
        tokenizer.save_pretrained(args.output_dir)

        # Good practice: save your training arguments together with the trained model
        torch.save(args, os.path.join(args.output_dir, 'training_args.bin'))

        # Load a trained model and vocabulary that you have fine-tuned
        model = model_class.from_pretrained(args.output_dir)
        tokenizer = tokenizer_class.from_pretrained(args.output_dir)
        model.to(args.device)

    # Evaluation
    results = {}
    if args.do_eval and args.local_rank in [-1, 0]:
        print("output_dir",args.output_dir)
        tokenizer = tokenizer_class.from_pretrained(args.output_dir, do_lower_case=args.do_lower_case)
        checkpoints = [args.output_dir]
        if args.eval_all_checkpoints:
            checkpoints = list(
                os.path.dirname(c) for c in sorted(glob.glob(args.output_dir + '/**/' + WEIGHTS_NAME, recursive=True)))
            logging.getLogger("transformers.modeling_utils").setLevel(logging.WARN)  # Reduce logging
        logger.info("Evaluate the following checkpoints: %s", checkpoints)

        for checkpoint in checkpoints:
            print("checkpoint",checkpoint)
            global_step = checkpoint.split('-')[-1] if len(checkpoints) > 1 else ""
            print("global_step",global_step)
            prefix = checkpoint.split("\\")[-1] if checkpoint.find('checkpoint') != -1 else ""
            print("prefix ",prefix)
            model = model_class.from_pretrained(checkpoint)
            if args.model_type == "bert":
                model.bert.encoder.set_early_exit_entropy(args.early_exit_entropy)
            else:
                model.roberta.encoder.set_early_exit_entropy(args.early_exit_entropy)
            model.to(args.device)
            result = evaluate(args, model, tokenizer, prefix=prefix,
                              eval_threshold=args.eval_threshold)
            print("result",result)
            print_result = get_wanted_result(result)
            print("Result: {}".format(print_result))
            if args.eval_each_layer:
                last_layer_results = print_result
                each_layer_results = []
                for i in range(model.num_layers):
                    print("layer:",i)
                    logger.info("\n")
                    _result = evaluate(args, model, tokenizer, prefix=prefix,
                                       output_layer=i, eval_threshold=args.eval_threshold)
                    print("result",_result)
                    if i + 1 < model.num_layers:
                        each_layer_results.append(get_wanted_result(_result))
                each_layer_results.append(last_layer_results)
                save_fname = args.plot_data_dir + '/' + args.model_name_or_path[2:] + "/each_layer.npy"
                if not os.path.exists(os.path.dirname(save_fname)):
                    os.makedirs(os.path.dirname(save_fname))
                np.save(save_fname, np.array(each_layer_results))
            result = dict((k + '_{}'.format(global_step), v) for k, v in result.items())
            results.update(result)

    return results


if __name__ == "__main__":
    main()
