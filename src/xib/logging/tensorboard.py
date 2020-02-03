from ignite.engine import Engine, Events
from ignite.contrib.handlers.base_logger import BaseHandler
from ignite.contrib.handlers.tensorboard_logger import TensorboardLogger
from ignite.contrib.handlers.tensorboard_logger import OptimizerParamsHandler as _OptimizerParamsHandler
from torch.optim .optimizer import Optimizer

from ..ignite import CustomEngine


class MetricsHandler(BaseHandler):
    """Handle all metrics, figures, images, and text saved in the state of an engine"""

    def __init__(self, tag: str, global_step_fn):
        self.tag = tag
        self.global_step_fn = global_step_fn

    def __call__(self, engine: CustomEngine, logger: TensorboardLogger, event_name: Events):
        global_step = self.global_step_fn()

        for name, value in engine.state.metrics.items():
            logger.writer.add_scalar(f'{self.tag}/{name}', value, global_step)

        for name, fig in engine.state.figures.items():
            logger.writer.add_figure(f'{self.tag}/{name}', fig, global_step, close=True)

        for name, img in engine.state.images.items():
            logger.writer.add_image(f'{self.tag}/{name}', img, global_step)

        for name, img in engine.state.text.items():
            logger.writer.add_text(f'{self.tag}/{name}', img, global_step)


class OptimizerParamsHandler(_OptimizerParamsHandler):
    def __init__(self, optimizer: Optimizer, global_step_fn, param_name='lr', tag=None):
        super(OptimizerParamsHandler, self).__init__(optimizer, param_name, tag)
        self.global_step_fn = global_step_fn

    def __call__(self, engine: Engine, logger: TensorboardLogger, event_name: Events):
        if not isinstance(logger, TensorboardLogger):
            raise RuntimeError("Handler 'OptimizerParamsHandler' works only with TensorboardLogger")

        global_step = self.global_step_fn()
        prefix = '{}/'.format(self.tag) if self.tag else ''

        params = {
            f'{prefix}{self.param_name}/group_{i}': float(pg[self.param_name])
            for i, pg in enumerate(self.optimizer.param_groups)
        }

        for k, v in params.items():
            logger.writer.add_scalar(k, v, global_step)


class EpochHandler(BaseHandler):
    def __init__(self, engine: Engine, global_step_fn):
        self.engine = engine
        self.global_step_fn = global_step_fn

    def __call__(self, engine: Engine, logger: TensorboardLogger, event_name: Events):
        logger.writer.add_scalar('z/epoch', engine.state.epoch, self.global_step_fn())
