import textwrap
from abc import ABC
from pathlib import Path
from typing import List, Tuple, Optional, Dict, Union, Iterator, Callable

import cv2
import torch
import numpy as np
import sklearn.metrics

from ignite.engine import Events, Engine
from ignite.metrics import Metric
from tensorboardX import SummaryWriter

from ..structures import ImageSize, Vocabulary


class BatchMetric(Metric, ABC):
    """A metric computed independently for every batch."""

    def attach(self, engine, name):
        # Reset at the beginning of every iteration
        if not engine.has_event_handler(self.started, Events.ITERATION_STARTED):
            engine.add_event_handler(Events.ITERATION_STARTED, self.started)
        # Update at the after every iteration
        if not engine.has_event_handler(self.iteration_completed, Events.ITERATION_COMPLETED):
            engine.add_event_handler(Events.ITERATION_COMPLETED, self.iteration_completed)
        # Copy metric to engine.state.metrics after every iteration
        engine.add_event_handler(Events.ITERATION_COMPLETED, self.completed, name)


def mean_average_precision(annotations, scores):
    """Computes the mean average precision (mAP) for a multi-class multi-label scenario.

    In object detection mAP is the average AP across all classes, i.e.
    for each class c, compute AP[c] using all samples in the dataset, then take the average across classes.

    Not to bo confounded with:
    for each sample s, compute the AP[s] considering all classes, then take the average across samples.
    """

    # The correct behavior (for each class compute the AP using all samples, then average across classes)
    # corresponds to the `macro` aggregation from scikit-learn.
    # However, if we are given a small batch it is possible to have a column of all 0s in the annotations matrix,
    # i.e. none of the samples is positive for that class. It's best to pass `average=None` so that per-class
    # APs are returned and then compute the mean manually skipping nan values.

    with np.errstate(invalid='ignore'):
        average_precisions = sklearn.metrics.average_precision_score(
            y_true=annotations,
            y_score=scores,
            average=None
        )

    return np.nanmean(average_precisions)


class MeanAveragePrecisionBatch(BatchMetric):
    avg_precision: float

    def reset(self):
        self.avg_precision = float('NaN')

    def update(self, output: Tuple[torch.Tensor, torch.Tensor]):
        y_true, y_score = output
        self.avg_precision = mean_average_precision(y_true, y_score)

    def compute(self):
        return self.avg_precision


class MeanAveragePrecisionEpoch(Metric):
    y_true: List[torch.Tensor]
    y_score: List[torch.Tensor]

    def reset(self):
        self.y_true = []
        self.y_score = []

    def update(self, output):
        y_true, y_score = output
        self.y_true.append(y_true)
        self.y_score.append(y_score)

    def compute(self):
        return mean_average_precision(
            torch.cat(self.y_true),
            torch.cat(self.y_score),
        )


def precision_at(annotations, scores, sizes):
    """Precision@x

    - rank the relationships by their score and keep the top x
    - compute how many of those retrieved relationships are actually relevant

    ::

                  # ( relevant items retrieved )
      Precision = ------------------------------ = P ( relevant | retrieved )
                      # ( retrieved items )
    """
    result = {}
    # Sorted labels are the indexes that would sort y_score, e.g.
    # [[ 10, 3, 4, ....., 5, 41 ],
    #  [  1, 2, 6, ....., 8, 78 ]]
    # means that for the first image class 10 is the top scoring class
    sorted_labels = torch.argsort(scores, dim=1, descending=True)

    # One could do this to get the sorted scores
    # sorted_scores = torch.gather(y_scores, index=sorted_labels, dim=1)

    # Use these indexes to index into y_true, but keep only max(sizes) columns
    annotations_of_top_max_s = torch.gather(annotations, index=sorted_labels[:, :max(sizes)], dim=1)

    # cumsum[i, j] = number of relevant items within the top j+1 retrieved items
    # Cast to float to avoid int/int division.
    cumsum = annotations_of_top_max_s.cumsum(dim=1).float()

    # Given a size s, `cumsum[i, s-1] / s` gives the precision for sample i.
    # Then we take the batch mean.
    for s in sizes:
        result[s] = (cumsum[:, (s - 1)] / s).mean(dim=0).item()

    return result


def recall_at(annotations, scores, sizes):
    """Recall@x

    - rank the relationships by their score and keep the top x
    - compute how many of the relevant relationships are among the retrieved

    ::

                # ( relevant items retrieved )
      Recall  = ------------------------------ = P ( retrieved | relevant )
                     # ( relevant items )

    References:

        `"Visual Relationship Detection with Language Priors" (Lu et al.) <https://arxiv.org/abs/1608.00187>`_
        describes `recall@x` as:

            The evaluation metrics we report is recall @ 100 and recall @ 50.
            Recall @ x computes the fraction of times the correct relationship is predicted
            in the top x confident relationship predictions. Since we have 70 predicates and
            an average of 18 objects per image, the total possible number of relationship
            predictions is 100×70×100, which implies that the random guess will result in a
            recall @ 100 of 0.00014.

        `Weakly-supervised learning of visual relations (Peyre et al.) <https://arxiv.org/abs/1707.09472>`_
        cites the previous paper and describes `recall@x` as:

            We compute recall @ x which corresponds to the proportion of ground truth
            pairs among the x top scored candidate pairs in each image.
    """
    result = {}
    # Sorted labels are the indexes that would sort y_score, e.g.
    # [[ 10, 3, 4, ....., 5, 41 ],
    #  [  1, 2, 6, ....., 8, 78 ]]
    # means that for the first image class 10 is the top scoring class
    sorted_labels = torch.argsort(scores, dim=1, descending=True)

    # One could do this to get the sorted scores
    # sorted_scores = torch.gather(y_scores, index=sorted_labels, dim=1)

    # Use these indexes to index into y_true, but keep only max(self.sizes) columns
    annotations_of_top_max_s = torch.gather(annotations, index=sorted_labels[:, :max(sizes)], dim=1)

    # cumsum[i, j] = number of relevant items within the top j+1 retrieved items
    # Cast to float to avoid int/int division.
    cumsum = annotations_of_top_max_s.cumsum(dim=1).float()

    # Divide each row by the total number of relevant document for that row to get the recall per sample.
    # Then take the batch mean.
    num_rel = annotations.sum(dim=1, keepdims=True)
    recall = cumsum / num_rel
    for s in sizes:
        result[s] = recall[:, s - 1].mean(axis=0).item()

    return result


class RecallAtBatch(BatchMetric):
    """Recall@x over the output of the last batch"""
    _recall_at: Dict[int, Optional[float]]

    def __init__(self, sizes: Tuple[int, ...] = (10, 30, 50), output_transform=lambda x: x, device=None):
        self._sorted_sizes = list(sorted(sizes))
        super(RecallAtBatch, self).__init__(output_transform, device)

    def reset(self):
        self._recall_at = {s: None for s in self._sorted_sizes}

    def update(self, output: Tuple[torch.Tensor, torch.Tensor]):
        y_true, y_score = output
        self._recall_at.update(recall_at(y_true, y_score, self._sorted_sizes))

    def compute(self):
        return self._recall_at

    def completed(self, engine, name):
        result = self.compute()
        for k, v in result.items():
            engine.state.metrics[f'{name}_{k}'] = v


class RecallAtEpoch(Metric):
    """Recall@x by accumulating outputs over epochs"""
    _y_true: List[torch.Tensor]
    _y_score: List[torch.Tensor]

    def __init__(self, sizes: Tuple[int, ...] = (10, 30, 50), output_transform=lambda x: x, device=None):
        self._sorted_sizes = list(sorted(sizes))
        super(RecallAtEpoch, self).__init__(output_transform, device)

    def reset(self):
        self._y_true = []
        self._y_score = []

    def update(self, output: Tuple[torch.Tensor, torch.Tensor]):
        y_true, y_score = output
        self._y_true.append(y_true)
        self._y_score.append(y_score)

    def compute(self):
        y_true = torch.cat(self._y_true, dim=0)
        y_score = torch.cat(self._y_score, dim=0)
        return recall_at(y_true, y_score, self._sorted_sizes)

    def completed(self, engine, name):
        result = self.compute()
        for k, v in result.items():
            engine.state.metrics[f'{name}_{k}'] = v


class PredictPredicatesImg(object):
    def __init__(
            self,
            grid: Tuple[int, int],
            img_dir: Union[str, Path],
            tag: str,
            logger: SummaryWriter,
            global_step_fn: Callable[[], int],
            predicate_vocabulary: Vocabulary,
            save_dir: Optional[Union[str, Path]] = None,
    ):
        """

        Args:
            grid:
            img_dir: directory where the images will be opened from
            tag:
            logger: tensorboard logger for the images
            global_step_fn:
            save_dir: optional destination for .jpg images
        """
        self.tag = tag
        self.grid = grid
        self.logger = logger
        self.global_step_fn = global_step_fn
        self.predicate_vocabulary = predicate_vocabulary

        self.img_dir = Path(img_dir).expanduser().resolve()
        if not self.img_dir.is_dir():
            raise ValueError(f'Image dir must exist: {self.img_dir}')

        self.save_dir = save_dir
        if self.save_dir is not None:
            self.save_dir = Path(self.logger.logdir).expanduser().resolve()
            self.save_dir.mkdir(parents=True, exist_ok=True)

    def __call__(self, engine: Engine):
        import matplotlib.pyplot as plt
        plt.switch_backend('Agg')

        global_step = self.global_step_fn()

        predicate_probs = engine.state.output['output'].sigmoid()
        targets_bce = engine.state.output['target']
        filenames = engine.state.batch[2]

        fig, axes = plt.subplots(*self.grid, figsize=(16, 12), dpi=50)
        axes_iter: Iterator[plt.Axes] = axes.flat

        for target, pred, filename, ax in zip(targets_bce, predicate_probs, filenames, axes_iter):
            image = cv2.imread(self.img_dir.joinpath(filename).as_posix())
            image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
            img_size = ImageSize(*image.shape[:2])

            recall_at_5 = recall_at(target[None, :], pred[None, :], (5,))[5]
            mAP = mean_average_precision(target[None, :], pred[None, :])

            ax.imshow(image)
            ax.set_title(f'{filename[:-4]} mAP {mAP:.1%} R@5 {recall_at_5:.1%}')

            target_str = self.predicate_vocabulary.get_str(target.nonzero().flatten()).tolist()
            ax.text(
                0.05, 0.95,
                '\n'.join(target_str),
                transform=ax.transAxes,
                fontsize=11,
                verticalalignment='top',
                bbox=dict(boxstyle='square', facecolor='white', alpha=0.8)
            )

            top_5 = torch.argsort(pred, descending=True)[:5]
            prediction_str = [f'{score:.1%} {str}' for score, str
                              in zip(pred[top_5], self.predicate_vocabulary.get_str(top_5))]
            ax.text(
                0.65, 0.95,
                '\n'.join(prediction_str),
                transform=ax.transAxes,
                fontsize=11,
                verticalalignment='top',
                bbox=dict(boxstyle='square', facecolor='white', alpha=0.8)
            )

            ax.tick_params(which='both', **{k: False for k in ('bottom', 'top', 'left', 'right',
                                                               'labelbottom', 'labeltop', 'labelleft', 'labelright')})
            ax.set_xlim(0, img_size.width)
            ax.set_ylim(img_size.height, 0)

        fig.tight_layout()

        if self.save_dir is not None:
            import io
            from PIL import Image
            with io.BytesIO() as buff:
                fig.savefig(buff, format='png', facecolor='white', bbox_inches='tight', dpi=50)
                pil_img = Image.open(buff).convert('RGB')
                plt.close(fig)
            save_path = self.save_dir.joinpath(f'{global_step}.{self.tag}.jpg')
            pil_img.save(save_path, 'JPEG')
            self.logger.add_image(f'{self.tag}', np.moveaxis(np.asarray(pil_img), 2, 0), global_step=global_step)
        else:
            self.logger.add_figure(f'{self.tag}', fig, global_step=global_step, close=True)


class PredictRelationsImg(object):
    def __init__(
            self,
            grid: Tuple[int, int],
            img_dir: Union[str, Path],
            tag: str,
            logger: SummaryWriter,
            top_x_relations: int,
            global_step_fn: Callable[[], int],
            object_vocabulary: Vocabulary,
            predicate_vocabulary: Vocabulary,
            save_dir: Optional[Union[str, Path]] = None,
    ):
        """

        Args:
            grid:
            img_dir: directory where the images will be opened from
            tag:
            logger: tensorboard logger for the images
            global_step_fn:
            save_dir: optional destination for .jpg images
        """
        self.tag = tag
        self.grid = grid
        self.logger = logger
        self.top_x_relations = top_x_relations
        self.global_step_fn = global_step_fn
        self.object_vocabulary = object_vocabulary
        self.predicate_vocabulary = predicate_vocabulary

        self.img_dir = Path(img_dir).expanduser().resolve()
        if not self.img_dir.is_dir():
            raise ValueError(f'Image dir must exist: {self.img_dir}')

        self.save_dir = save_dir
        if self.save_dir is not None:
            self.save_dir = Path(self.logger.logdir).expanduser().resolve()
            self.save_dir.mkdir(parents=True, exist_ok=True)

    def __call__(self, engine: Engine):
        # import matplotlib.pyplot as plt
        # plt.switch_backend('Agg')
        #
        global_step = self.global_step_fn()

        text = ''

        relations = engine.state.output['relations']
        targets = engine.state.batch[1]
        filenames = engine.state.batch[2]

        pred_relation_scores = torch.split_with_sizes(relations.relation_scores, relations.n_edges.tolist())
        pred_predicate_scores = torch.split_with_sizes(relations.predicate_scores, relations.n_edges.tolist())
        pred_predicate_classes = torch.split_with_sizes(relations.predicate_classes, relations.n_edges.tolist())
        pred_relation_indexes = torch.split_with_sizes(relations.relation_indexes, relations.n_edges.tolist(), dim=1)

        gt_predicate_classes = torch.split_with_sizes(targets.predicate_classes, targets.n_edges.tolist())
        gt_relation_indexes = torch.split_with_sizes(targets.relation_indexes, targets.n_edges.tolist(), dim=1)

        for b in range(min(relations.num_graphs, self.grid[0] * self.grid[1])):
            buffer = f'{filenames[b]}\n\n'

            top_x_relations = set()
            count_retrieved = 0
            buffer += f'Top {relations.n_edges[b].item()} relations:\n'
            for i in range(relations.n_edges[b].item()):
                score = pred_relation_scores[b][i].item()

                subj_idx = pred_relation_indexes[b][0, i].item()
                obj_idx = pred_relation_indexes[b][1, i].item()
                predicate_score = pred_predicate_scores[b][i].item()
                predicate_class = pred_predicate_classes[b][i].item()
                predicate_str = self.predicate_vocabulary.get_str(predicate_class)

                subj_class = relations.object_classes[pred_relation_indexes[b][0, i]].item()
                obj_class = relations.object_classes[pred_relation_indexes[b][1, i]].item()
                subj_box = relations.object_boxes[pred_relation_indexes[b][0, i]].cpu().numpy().round(1)
                obj_box = relations.object_boxes[pred_relation_indexes[b][1, i]].cpu().numpy().round(1)
                subj_str = self.object_vocabulary.get_str(subj_class)
                obj_str = self.object_vocabulary.get_str(obj_class)

                top_x_relations.add((subj_class, subj_idx, predicate_class, obj_idx, obj_class))
                buffer += (
                    f'{i + 1:3d} {score:.1e} : '
                    f'({subj_idx:3d}) {subj_str:<14} {predicate_str:^14} {obj_str:>14} ({obj_idx:3d})'
                    f'   {str(subj_box):<25} {predicate_score:>6.1%} {str(obj_box):>25}\n'
                )

            buffer += f'\nGround-truth relations:\n'
            for j in range(targets.n_edges[b].item()):
                subj_idx = gt_relation_indexes[b][0, j].item()
                obj_idx = gt_relation_indexes[b][1, j].item()
                predicate_class = gt_predicate_classes[b][j].item()
                predicate_str = self.predicate_vocabulary.get_str(predicate_class)

                subj_class = targets.object_classes[gt_relation_indexes[b][0, j]].item()
                obj_class = targets.object_classes[gt_relation_indexes[b][1, j]].item()
                subj_box = targets.object_boxes[gt_relation_indexes[b][0, j]].cpu().numpy().round(1)
                obj_box = targets.object_boxes[gt_relation_indexes[b][1, j]].cpu().numpy().round(1)
                subj_str = self.object_vocabulary.get_str(subj_class)
                obj_str = self.object_vocabulary.get_str(obj_class)

                # Assume the input boxes are from GT, not detectron, otherwise we'd have to match by IoU
                retrieved = (subj_class, subj_idx, predicate_class, obj_idx, obj_class) in top_x_relations
                if retrieved:
                    count_retrieved += 1
                buffer += (
                    f'  {"OK️" if retrieved else "  "}        : '
                    f'({subj_idx:3d}) {subj_str:<14} {predicate_str:^14} {obj_str:>14} ({obj_idx:3d})'
                    f'   {str(subj_box):<25}        {str(obj_box):>25}\n'
                )

            buffer += f'\nRecall@{self.top_x_relations}: {count_retrieved/targets.n_edges[b].item():.2%}\n\n'

            text += textwrap.indent(buffer, '    ', lambda line: True) + '---\n\n'

        self.logger.add_text('Visual relations', text, global_step=global_step)

        # fig, axes = plt.subplots(*self.grid, figsize=(16, 12), dpi=50)
        # axes_iter: Iterator[plt.Axes] = axes.flat

        # for target, pred, filename, ax in zip(targets_bce, predicate_probs, filenames, axes_iter):
        #     image = cv2.imread(self.img_dir.joinpath(filename).as_posix())
        #     image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        #     img_size = ImageSize(*image.shape[:2])
        #
        #     recall_at_5 = recall_at(target[None, :], pred[None, :], (5,))[5]
        #     mAP = mean_average_precision(target[None, :], pred[None, :])
        #
        #     ax.imshow(image)
        #     ax.set_title(f'{filename[:-4]} mAP {mAP:.1%} R@5 {recall_at_5:.1%}')
        #
        #     target_str = self.predicate_vocabulary.get_str(target.nonzero().flatten()).tolist()
        #     ax.text(
        #         0.05, 0.95,
        #         '\n'.join(target_str),
        #         transform=ax.transAxes,
        #         fontsize=11,
        #         verticalalignment='top',
        #         bbox=dict(boxstyle='square', facecolor='white', alpha=0.8)
        #     )
        #
        #     top_5 = torch.argsort(pred, descending=True)[:5]
        #     prediction_str = [f'{score:.1%} {str}' for score, str
        #                       in zip(pred[top_5], self.predicate_vocabulary.get_str(top_5))]
        #     ax.text(
        #         0.65, 0.95,
        #         '\n'.join(prediction_str),
        #         transform=ax.transAxes,
        #         fontsize=11,
        #         verticalalignment='top',
        #         bbox=dict(boxstyle='square', facecolor='white', alpha=0.8)
        #     )
        #
        #     ax.tick_params(which='both', **{k: False for k in ('bottom', 'top', 'left', 'right',
        #                                                        'labelbottom', 'labeltop', 'labelleft', 'labelright')})
        #     ax.set_xlim(0, img_size.width)
        #     ax.set_ylim(img_size.height, 0)
        #
        # fig.tight_layout()
        #
        # if self.save_dir is not None:
        #     import io
        #     from PIL import Image
        #     with io.BytesIO() as buff:
        #         fig.savefig(buff, format='png', facecolor='white', bbox_inches='tight', dpi=50)
        #         pil_img = Image.open(buff).convert('RGB')
        #         plt.close(fig)
        #     save_path = self.save_dir.joinpath(f'{global_step}.{self.tag}.jpg')
        #     pil_img.save(save_path, 'JPEG')
        #     self.logger.add_image(f'{self.tag}', np.moveaxis(np.asarray(pil_img), 2, 0), global_step=global_step)
        # else:
        #     self.logger.add_figure(f'{self.tag}', fig, global_step=global_step, close=True)
