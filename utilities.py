import os
import re
import csv
import signal
import torch
import random
import numpy as np
import json
import argparse
from tabulate import tabulate
from itertools import groupby
from collections import defaultdict
from seqeval.metrics import f1_score, precision_score, recall_score
from seqeval.metrics import classification_report
from typing import List, Dict


def report2dict(cr):
    # Parse rows
    tmp = list()
    for row in cr.split("\n"):
        parsed_row = [x.strip() for x in row.split("  ") if len(x) > 0]
        if len(parsed_row) > 0:
            tmp.append(parsed_row)

    # Store in dictionary
    measures = tmp[0]

    D_class_data = defaultdict(dict)
    for row in tmp[1:]:
        class_label = row[0]
        for j, m in enumerate(measures):
            D_class_data[class_label][m.strip()] = float(row[j + 1].strip())
    return D_class_data


def printcr(report, classes=None, sort_by_support=False):
    headers = ['classes', 'precision', 'recall', 'f1-score', 'support']

    if classes is None:
        classes = [k for k in report.keys() if k not in {'macro avg', 'micro avg'}]

        if sort_by_support:
              classes = sorted(classes, key=lambda c: report[c]['support'], reverse=True)
        else: classes = sorted(classes)

    if 'macro avg' not in classes: classes.append('macro avg')
    if 'micro avg' not in classes: classes.append('micro avg')

    table = []
    for c in classes:
        if c == 'macro avg':
            table.append([])
        row = [c]
        for h in headers:
            if h not in report[c]:
                continue
            if h in {'precision', 'recall', 'f1-score'}:
                  row.append(report[c][h] * 100)
            else: row.append(report[c][h])
        table.append(row)
    print(tabulate(table, headers=headers, floatfmt=(".3f", ".3f", ".3f", ".3f")))
    print()


class EpochStats:
    def __init__(self):
        self.sizes = [] # number of elements per step
        self.losses = []
        self.ner_losses = []
        self.dep_losses = []

        self.probs = []
        self.preds = []
        self.golds = []

    def loss_step(self, loss: float, ner_loss: float, dep_loss: float, batch_size: int):
        self.losses.append(loss)
        self.ner_losses.append(ner_loss)
        self.dep_losses.append(dep_loss)
        self.sizes.append(batch_size)

    def step(self, scores, target, mask, loss, ner_loss, dep_loss):
        self.loss_step(loss, ner_loss, dep_loss, len(scores))

        probs, classes = scores.max(dim=2)

        for i in range(len(scores)):
            prob_i = probs[i][mask[i] == 1].cpu().tolist()
            pred_i = classes[i][mask[i] == 1].cpu().tolist()
            gold_i = target[i][mask[i] == 1].cpu().tolist()

            self.preds.append(pred_i) # self.preds.extend(pred_i)
            self.golds.append(gold_i) # self.golds.extend(gold_i)
            self.probs.append(prob_i) # self.probs.extend(prob_i)

    def loss(self, loss_type: str = ''):
        if loss_type == 'ner':
            losses = self.ner_losses
        elif loss_type == 'dep':
            losses = self.dep_losses
        else:
            losses = self.losses
        return np.mean([l for l, s in zip(losses, self.sizes) for _ in range(s)]), np.min(losses), np.max(losses)

    def _map_to_labels(self, index2label):
        # Predictions should have been as nested list to separate predictions
        # Since we store the predictions across epochs during training, we need to wrap up this in a try except
        # so that it handles the flattened lists in case they are not nested. New runs will be nested
        try:
            golds = [[index2label[j] for j in i] for i in self.golds]
            preds = [[index2label[j] for j in i] for i in self.preds]
        except TypeError:
            golds = [index2label[i] for i in self.golds]
            preds = [index2label[i] for i in self.preds]
        return golds, preds

    def metrics(self, index2label: [List[str], Dict[int, str]]):
        golds, preds =self._map_to_labels(index2label)

        f1 = f1_score(golds, preds)
        p = precision_score(golds, preds)
        r = recall_score(golds, preds)

        return f1, p, r

    def get_classification_report(self, index2label: [List[str], Dict[int, str]]):
        golds, preds = self._map_to_labels(index2label)

        cr = classification_report(golds, preds, digits=5)
        return report2dict(cr)

    def print_classification_report(self, index2label: [List[str], Dict[int, str]] = None, report = None):
        assert index2label is not None or report is not None

        if report is None:
            report = self.get_classification_report(index2label)

        printcr(report)


def set_seed(seed):
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)


def read_conll(filename, columns, delimiter='\t'):
    def is_empty_line(line_pack):
        return all(field.strip() == '' for field in line_pack)

    data = []
    with open(filename) as fp:
        reader = csv.reader(fp, delimiter=delimiter, quoting=csv.QUOTE_NONE)
        groups = groupby(reader, is_empty_line)

        for is_empty, pack in groups:
            if is_empty is False:
                data.append([list(field) for field in zip(*pack)])

    data = list(zip(*data))
    dataset = {colname: list(data[columns[colname]]) for colname in columns}

    return dataset

def write_conll(filename, data, colnames: List[str] = None, delimiter='\t'):
    os.makedirs(os.path.dirname(filename), exist_ok=True)

    if colnames is None:
        colnames = list(data.keys())

    any_key = colnames[0]

    with open(filename, 'w') as fp:
        for sample_i in range(len(data[any_key])):
            for token_i in range(len(data[any_key][sample_i])):
                row = [data[col][sample_i][token_i] for col in colnames]
                fp.write(delimiter.join(row) + '\n')
            fp.write('\n')


def read_conll_corpus(corpus_dir, filenames, columns, delimiter='\t'):
    corpus = {}
    for datafile in filenames:
        dataset = os.path.splitext(datafile)[0]
        datafile = os.path.join(corpus_dir, datafile)
        corpus[dataset] = read_conll(datafile, columns, delimiter=delimiter)
    return corpus


def flatten(nested_elems):
    return [elem for elems in nested_elems for elem in elems]



class Arguments(dict):
    def __init__(self, *args, **kwargs):
        super(Arguments, self).__init__(*args, **kwargs)
        self.__dict__ = self

    @staticmethod
    def from_nested_dict(data):
        if not isinstance(data, dict):
              return data
        else: return Arguments({key: Arguments.from_nested_dict(data[key]) for key in data})


def load_args(default_config=None, verbose=False):
    parser = argparse.ArgumentParser()

    # Required parameters
    parser.add_argument('--config', default=default_config, type=str, required=default_config is None, help='Provide the JSON config file with the experiment parameters')

    if default_config is None:
        args = parser.parse_args()
    else:
        args = parser.parse_args("")

    # Override the default values with the JSON arguments
    with open(args.config) as f:
        params = ''.join([re.sub(r"//.*$", "", line, flags=re.M) for line in f])  # Remove comments from the JSON config
        args = Arguments.from_nested_dict(json.loads(params))


    # Exp Args
    args.experiment.output_dir = os.path.join("results", args.experiment.id)
    args.experiment.checkpoint_dir = os.path.join(args.experiment.output_dir, "checkpoint")

    # Optim Args
    args.optim.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    args.optim.n_gpu = torch.cuda.device_count()

    if verbose:
        for main_field in ['experiment', 'data', 'preproc', 'model', 'optim']:
            assert hasattr(args, main_field)
            print(f"{main_field.title()} Args:")
            for k,v in args[main_field].items():
                print(f"\t{k}: {v}")
            print()
    return args
