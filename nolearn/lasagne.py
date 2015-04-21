from __future__ import absolute_import

from ._compat import pickle
from collections import OrderedDict
from difflib import SequenceMatcher
import itertools as it
import operator as op
from time import time
import pdb

from lasagne.layers import Conv2DLayer
from lasagne.layers import MaxPool2DLayer
from lasagne.objectives import categorical_crossentropy
from lasagne.objectives import mse
from lasagne.objectives import Objective
from lasagne.updates import nesterov_momentum
from lasagne.utils import unique
import matplotlib.pyplot as plt
import numpy as np
from sklearn.base import BaseEstimator
from sklearn.cross_validation import KFold
from sklearn.cross_validation import StratifiedKFold
from sklearn.metrics import accuracy_score
from sklearn.metrics import mean_squared_error
from sklearn.preprocessing import LabelEncoder
from tabulate import tabulate
import theano
from theano import tensor as T
try:
    from lasagne.layers.cuda_convnet import Conv2DCCLayer
    from lasagne.layers.cuda_convnet import MaxPool2DCCLayer
except ImportError:
    Conv2DCCLayer = Conv2DLayer
    MaxPool2DCCLayer = MaxPool2DLayer


class _list(list):
    pass


class _dict(dict):
    def __contains__(self, key):
        return True


class ansi:
    BLUE = '\033[94m'
    CYAN = '\033[36m'
    GREEN = '\033[32m'
    MAGENTA = '\033[35m'
    RED = '\033[31m'
    ENDC = '\033[0m'


class BatchIterator(object):
    def __init__(self, batch_size):
        self.batch_size = batch_size

    def __call__(self, X, y=None):
        self.X, self.y = X, y
        return self

    def __iter__(self):
        n_samples = self.X.shape[0]
        bs = self.batch_size
        for i in range((n_samples + bs - 1) // bs):
            sl = slice(i * bs, (i + 1) * bs)
            Xb = self.X[sl]
            if self.y is not None:
                yb = self.y[sl]
            else:
                yb = None
            yield self.transform(Xb, yb)

    def transform(self, Xb, yb):
        return Xb, yb


def get_real_filter(layers, img_size):
    """Get the real filter sizes of each layer involved in
    convoluation. See Xudong Cao:
    https://www.kaggle.com/c/datasciencebowl/forums/t/13166/happy-lantern-festival-report-and-code

    This does not yet take into consideration feature pooling,
    padding, striding and similar gimmicks.

    """
    # imports here to prevent circular dependencies
    real_filter = np.zeros((len(layers), 2))
    conv_mode = True
    first_conv_layer = True
    expon = np.ones((1, 2))

    for i, layer in enumerate(layers[1:]):
        j = i + 1
        if not conv_mode:
            real_filter[j] = img_size
            continue

        if (isinstance(layer, Conv2DLayer) or
            isinstance(layer, Conv2DCCLayer)):
            if not first_conv_layer:
                new_filter = np.array(layer.filter_size) * expon
                real_filter[j] = new_filter
            else:
                new_filter = np.array(layer.filter_size) * expon
                real_filter[j] = new_filter
                first_conv_layer = False
        elif (isinstance(layer, MaxPool2DLayer) or
              isinstance(layer, MaxPool2DCCLayer)):
            real_filter[j] = real_filter[i]
            expon *= np.array(layer.ds)
        else:
            conv_mode = False
            real_filter[j] = img_size

    real_filter[0] = img_size
    return real_filter


def get_receptive_field(layers, img_size):
    """Get the real filter sizes of each layer involved in
    convoluation. See Xudong Cao:
    https://www.kaggle.com/c/datasciencebowl/forums/t/13166/happy-lantern-festival-report-and-code

    This does not yet take into consideration feature pooling,
    padding, striding and similar gimmicks.

    """
    receptive_field = np.zeros((len(layers), 2))
    conv_mode = True
    first_conv_layer = True
    expon = np.ones((1, 2))

    for i, layer in enumerate(layers[1:]):
        j = i + 1
        if not conv_mode:
            receptive_field[j] = img_size
            continue

        if (isinstance(layer, Conv2DLayer) or
            isinstance(layer, Conv2DCCLayer)):
            if not first_conv_layer:
                last_field = receptive_field[i]
                new_field = (last_field + expon *
                             (np.array(layer.filter_size) - 1))
                receptive_field[j] = new_field
            else:
                receptive_field[j] = layer.filter_size
                first_conv_layer = False
        elif (isinstance(layer, MaxPool2DLayer) or
              isinstance(layer, MaxPool2DCCLayer)):
            receptive_field[j] = receptive_field[i]
            expon *= np.array(layer.ds)
        else:
            conv_mode = False
            receptive_field[j] = img_size

    receptive_field[0] = img_size
    return receptive_field


def get_conv_infos(net, min_capacity=100. / 6, tablefmt='pipe',
                   detailed=False):
    CYA = ansi.CYAN
    END = ansi.ENDC
    MAG = ansi.MAGENTA
    RED = ansi.RED

    layers = net.layers_.values()
    img_size = net.layers_['input'].get_output_shape()[2:]

    header = ['name', 'size', 'total', 'cap. Y [%]', 'cap. X [%]',
              'cov. Y [%]', 'cov. X [%]']
    if detailed:
        header += ['filter Y', 'filter X', 'field Y', 'field X']

    shapes = [layer.get_output_shape()[1:] for layer in layers]
    totals = [str(reduce(op.mul, shape)) for shape in shapes]
    shapes = ['x'.join(map(str, shape)) for shape in shapes]
    shapes = np.array(shapes).reshape(-1, 1)
    totals = np.array(totals).reshape(-1, 1)

    real_filters = get_real_filter(layers, img_size)
    receptive_fields = get_receptive_field(layers, img_size)
    capacity = 100. * real_filters / receptive_fields
    capacity[np.negative(np.isfinite(capacity))] = 1
    img_coverage = 100. * receptive_fields / img_size
    layer_names = [layer.name if layer.name
                   else str(layer).rsplit('.')[-1].split(' ')[0]
                   for layer in layers]

    colored_names = []
    for name, (covy, covx), (capy, capx) in zip(
            layer_names, img_coverage, capacity):
        if (
                ((covy > 100) or (covx > 100)) and
                ((capy < min_capacity) or (capx < min_capacity))
        ):
            name = "{}{}{}".format(RED, name, END)
        elif (covy > 100) or (covx > 100):
            name = "{}{}{}".format(CYA, name, END)
        elif (capy < min_capacity) or (capx < min_capacity):
            name = "{}{}{}".format(MAG, name, END)
        colored_names.append(name)
    colored_names = np.array(colored_names).reshape(-1, 1)

    table = np.hstack((colored_names, shapes, totals, capacity, img_coverage))
    if detailed:
        table = np.hstack((table, real_filters.astype(int),
                           receptive_fields.astype(int)))

    return tabulate(table, header, tablefmt=tablefmt, floatfmt='.2f')


class NeuralNet(BaseEstimator):
    """A scikit-learn estimator based on Lasagne.
    """
    def __init__(
        self,
        layers,
        update=nesterov_momentum,
        loss=None,
        objective=Objective,
        objective_loss_function=None,
        batch_iterator_train=BatchIterator(batch_size=128),
        batch_iterator_test=BatchIterator(batch_size=128),
        regression=False,
        max_epochs=100,
        eval_size=0.2,
        custom_score=None,
        X_tensor_type=None,
        y_tensor_type=None,
        use_label_encoder=False,
        on_epoch_finished=(),
        on_training_finished=(),
        more_params=None,
        verbose=0,
        **kwargs
        ):
        if loss is not None:
            raise ValueError(
                "The 'loss' parameter was removed, please use "
                "'objective_loss_function' instead.")  # BBB
        if objective_loss_function is None:
            objective_loss_function = (
                mse if regression else categorical_crossentropy)

        if X_tensor_type is None:
            types = {
                2: T.matrix,
                3: T.tensor3,
                4: T.tensor4,
                }
            X_tensor_type = types[len(kwargs['input_shape'])]
        if y_tensor_type is None:
            y_tensor_type = T.fmatrix if regression else T.ivector

        self.layers = layers
        self.update = update
        self.objective = objective
        self.objective_loss_function = objective_loss_function
        self.batch_iterator_train = batch_iterator_train
        self.batch_iterator_test = batch_iterator_test
        self.regression = regression
        self.max_epochs = max_epochs
        self.eval_size = eval_size
        self.custom_score = custom_score
        self.X_tensor_type = X_tensor_type
        self.y_tensor_type = y_tensor_type
        self.use_label_encoder = use_label_encoder
        self.on_epoch_finished = on_epoch_finished
        self.on_training_finished = on_training_finished
        self.more_params = more_params or {}
        self.verbose = verbose

        for key in kwargs.keys():
            assert not hasattr(self, key)
        vars(self).update(kwargs)
        self._kwarg_keys = list(kwargs.keys())
        self._check_for_unused_kwargs()

        self.train_history_ = []

        if 'batch_iterator' in kwargs:  # BBB
            raise ValueError(
                "The 'batch_iterator' argument has been replaced. "
                "Use 'batch_iterator_train' and 'batch_iterator_test' instead."
                )

    def _check_for_unused_kwargs(self):
        names = [n for n, _ in self.layers] + ['update', 'objective']
        for k in self._kwarg_keys:
            for n in names:
                prefix = '{}_'.format(n)
                if k.startswith(prefix):
                    break
            else:
                raise ValueError("Unused kwarg: {}".format(k))

    def initialize(self):
        if getattr(self, '_initialized', False):
            return

        out = getattr(self, '_output_layer', None)
        if out is None:
            out = self._output_layer = self.initialize_layers()

        iter_funcs = self._create_iter_funcs(
            self.layers_, self.objective, self.update,
            self.X_tensor_type,
            self.y_tensor_type,
            )
        self.train_iter_, self.eval_iter_, self.predict_iter_ = iter_funcs
        self._initialized = True

        if self.verbose:
            self._print_layer_info()

    def _get_params_for(self, name):
        collected = {}
        prefix = '{}_'.format(name)

        params = vars(self)
        more_params = self.more_params

        for key, value in it.chain(params.items(), more_params.items()):
            if key.startswith(prefix):
                collected[key[len(prefix):]] = value

        return collected

    def initialize_layers(self, layers=None):
        if layers is not None:
            self.layers = layers

        self.layers_ = OrderedDict()
        input_layer_name, input_layer_factory = self.layers[0]
        input_layer_params = self._get_params_for(input_layer_name)
        layer = input_layer_factory(
            name=input_layer_name, **input_layer_params)
        self.layers_[input_layer_name] = layer

        for (layer_name, layer_factory) in self.layers[1:]:
            layer_params = self._get_params_for(layer_name)
            incoming = layer_params.pop('incoming', None)
            if incoming is not None:
                if isinstance(incoming, (list, tuple)):
                    layer = [self.layers_[name] for name in incoming]
                else:
                    layer = self.layers_[incoming]
            layer = layer_factory(layer, name=layer_name, **layer_params)
            self.layers_[layer_name] = layer

        return self.layers_['output']

    def _create_iter_funcs(self, layers, objective, update, input_type,
                           output_type):
        X = input_type('x')
        y = output_type('y')
        X_batch = input_type('x_batch')
        y_batch = output_type('y_batch')

        output_layer = layers['output']
        objective_params = self._get_params_for('objective')
        obj = objective(output_layer, **objective_params)
        if not hasattr(obj, 'layers'):
            # XXX breaking the Lasagne interface a little:
            obj.layers = layers

        loss_train = obj.get_loss(X_batch, y_batch)
        loss_eval = obj.get_loss(X_batch, y_batch, deterministic=True)
        predict_proba = output_layer.get_output(X_batch, deterministic=True)
        if not self.regression:
            predict = predict_proba.argmax(axis=1)
            accuracy = T.mean(T.eq(predict, y_batch))
        else:
            accuracy = loss_eval

        all_params = self.get_all_params()
        update_params = self._get_params_for('update')
        updates = update(loss_train, all_params, **update_params)

        train_iter = theano.function(
            inputs=[theano.Param(X_batch), theano.Param(y_batch)],
            outputs=[loss_train],
            updates=updates,
            givens={
                X: X_batch,
                y: y_batch,
                },
            )
        eval_iter = theano.function(
            inputs=[theano.Param(X_batch), theano.Param(y_batch)],
            outputs=[loss_eval, accuracy],
            givens={
                X: X_batch,
                y: y_batch,
                },
            )
        predict_iter = theano.function(
            inputs=[theano.Param(X_batch)],
            outputs=predict_proba,
            givens={
                X: X_batch,
                },
            )

        return train_iter, eval_iter, predict_iter

    def fit(self, X, y):
        if self.use_label_encoder:
            self.enc_ = LabelEncoder()
            y = self.enc_.fit_transform(y).astype(np.int32)
            self.classes_ = self.enc_.classes_
        self.initialize()

        try:
            self.train_loop(X, y)
        except KeyboardInterrupt:
            pass
        return self

    def train_loop(self, X, y):
        X_train, X_valid, y_train, y_valid = self.train_test_split(
            X, y, self.eval_size)

        on_epoch_finished = self.on_epoch_finished
        if not isinstance(on_epoch_finished, (list, tuple)):
            on_epoch_finished = [on_epoch_finished]

        on_training_finished = self.on_training_finished
        if not isinstance(on_training_finished, (list, tuple)):
            on_training_finished = [on_training_finished]

        epoch = 0
        best_valid_loss = (
            min([row['valid loss'] for row in self.train_history_]) if
            self.train_history_ else np.inf
            )
        best_train_loss = (
            min([row['train loss'] for row in self.train_history_]) if
            self.train_history_ else np.inf
            )
        first_iteration = True
        num_epochs_past = len(self.train_history_)

        while epoch < self.max_epochs:
            epoch += 1

            train_losses = []
            valid_losses = []
            valid_accuracies = []
            custom_score = []

            t0 = time()

            for Xb, yb in self.batch_iterator_train(X_train, y_train):
                batch_train_loss = self.train_iter_(Xb, yb)
                train_losses.append(batch_train_loss)

            for Xb, yb in self.batch_iterator_test(X_valid, y_valid):
                batch_valid_loss, accuracy = self.eval_iter_(Xb, yb)
                valid_losses.append(batch_valid_loss)
                valid_accuracies.append(accuracy)
                if self.custom_score:
                    y_prob = self.predict_iter_(Xb)
                    custom_score.append(self.custom_score[1](yb, y_prob))

            avg_train_loss = np.mean(train_losses)
            avg_valid_loss = np.mean(valid_losses)
            avg_valid_accuracy = np.mean(valid_accuracies)
            if custom_score:
                avg_custom_score = np.mean(custom_score)

            if avg_train_loss < best_train_loss:
                best_train_loss = avg_train_loss
            if avg_valid_loss < best_valid_loss:
                best_valid_loss = avg_valid_loss
            best_train_loss == avg_train_loss
            best_valid = best_valid_loss == avg_valid_loss

            info = OrderedDict([
                ('epoch', num_epochs_past + epoch),
                ('train loss', avg_train_loss),
                ('valid loss', avg_valid_loss),
                ('valid best', avg_valid_loss if best_valid else None),
                ('train/val', avg_train_loss / avg_valid_loss),
                ('valid acc', avg_valid_accuracy),
                ])
            if self.custom_score:
                info.update({self.custom_score[0]: avg_custom_score})
            info.update({'dur': time() - t0})

            self.train_history_.append(info)
            self.log_ = tabulate(self.train_history_, headers='keys',
                                 tablefmt='pipe', floatfmt='.4f')
            if self.verbose:
                if first_iteration:
                    print(self.log_.split('\n', 2)[0])
                    print(self.log_.split('\n', 2)[1])
                    first_iteration = False
                print(self.log_.rsplit('\n', 1)[-1])

            try:
                for func in on_epoch_finished:
                    func(self, self.train_history_)
            except StopIteration:
                break

        for func in on_training_finished:
            func(self, self.train_history_)

    def predict_proba(self, X):
        probas = []
        for Xb, yb in self.batch_iterator_test(X):
            probas.append(self.predict_iter_(Xb))
        return np.vstack(probas)

    def predict(self, X):
        if self.regression:
            return self.predict_proba(X)
        else:
            y_pred = np.argmax(self.predict_proba(X), axis=1)
            if self.use_label_encoder:
                y_pred = self.enc_.inverse_transform(y_pred)
            return y_pred

    def score(self, X, y):
        score = mean_squared_error if self.regression else accuracy_score
        return float(score(self.predict(X), y))

    def train_test_split(self, X, y, eval_size):
        if eval_size:
            if self.regression:
                kf = KFold(y.shape[0], round(1. / eval_size))
            else:
                kf = StratifiedKFold(y, round(1. / eval_size))

            train_indices, valid_indices = next(iter(kf))
            X_train, y_train = X[train_indices], y[train_indices]
            X_valid, y_valid = X[valid_indices], y[valid_indices]
        else:
            X_train, y_train = X, y
            X_valid, y_valid = X[len(X):], y[len(y):]

        return X_train, X_valid, y_train, y_valid

    def get_all_layers(self):
        return self.layers_.values()

    def get_all_params(self):
        layers = self.get_all_layers()
        params = sum([l.get_params() for l in layers], [])
        return unique(params)

    def __getstate__(self):
        state = dict(self.__dict__)
        for attr in (
            'train_iter_',
            'eval_iter_',
            'predict_iter_',
            '_initialized',
            ):
            if attr in state:
                del state[attr]
        return state

    def __setstate__(self, state):
        self.__dict__.update(state)
        self.initialize()

    def _print_layer_info(self):
        shapes = [param.get_value().shape for param in
                  self.get_all_params() if param]
        nparams = reduce(op.add, [reduce(op.mul, shape) for
                                  shape in shapes])
        print("# Neural Network with {} learnable parameters"
              "\n".format(nparams))
        print("## Layer information")

        layers = self.layers_.values()
        has_conv2d = any([isinstance(layer, Conv2DLayer) or
                          isinstance(layer, Conv2DCCLayer)
                          for layer in layers])
        if has_conv2d:
            self._print_layer_info_conv()
        else:
            self._print_layer_info_plain()

    def _print_layer_info_plain(self):
        nums = range(len(self.layers))
        names = list(zip(*self.layers))[0]
        output_shapes = ['x'.join(map(str, layer.get_output_shape()[1:]))
                         for layer in self.layers_.values()]
        table = OrderedDict([
            ('#', nums),
            ('name', names),
            ('size', output_shapes),
        ])
        self.layer_infos_ = tabulate(table, 'keys', tablefmt='pipe')
        print(self.layer_infos_)
        print("")

    def _print_layer_info_conv(self):
        if self.verbose > 1:
            detailed = True
            tablefmt = 'simple'
        else:
            detailed = False
            tablefmt = 'pipe'

        self.layer_infos_ = get_conv_infos(self, detailed=detailed,
                                           tablefmt=tablefmt)
        print(self.layer_infos_)
        print("\nExplanation")
        print("    X, Y:    image dimensions")
        print("    cap.:    learning capacity")
        print("    cov.:    coverage of image")
        print("    {}: capacity too low (<1/6)"
              "".format("{}{}{}".format(ansi.MAGENTA, "magenta", ansi.ENDC)))
        print("    {}:    image coverage too high (>100%)"
              "".format("{}{}{}".format(ansi.CYAN, "cyan", ansi.ENDC)))
        print("    {}:     capacity too low and coverage too high\n"
              "".format("{}{}{}".format(ansi.RED, "red", ansi.ENDC)))

    def get_params(self, deep=True):
        params = super(NeuralNet, self).get_params(deep=deep)

        # Incidentally, Lasagne layers have a 'get_params' too, which
        # for sklearn's 'clone' means it would treat it in a special
        # way when cloning.  Wrapping the list of layers in a custom
        # list type does the trick here, but of course it's crazy:
        params['layers'] = _list(params['layers'])
        return _dict(params)

    def _get_param_names(self):
        # This allows us to have **kwargs in __init__ (woot!):
        param_names = super(NeuralNet, self)._get_param_names()
        return param_names + self._kwarg_keys

    def save_weights_to(self, fname):
        weights = [w.get_value() for w in self.get_all_params()]
        with open(fname, 'wb') as f:
            pickle.dump(weights, f, -1)

    @staticmethod
    def _param_alignment(shapes0, shapes1):
        shapes0 = list(map(str, shapes0))
        shapes1 = list(map(str, shapes1))
        matcher = SequenceMatcher(a=shapes0, b=shapes1)
        matches = []
        for block in matcher.get_matching_blocks():
            if block.size == 0:
                continue
            matches.append((list(range(block.a, block.a + block.size)),
                            list(range(block.b, block.b + block.size))))
        result = [line for match in matches for line in zip(*match)]
        return result

    def load_weights_from(self, src):
        if not hasattr(self, '_initialized'):
            raise AttributeError(
                "Please initialize the net before loading weights.")

        if isinstance(src, str):
            src = np.load(src)
        if isinstance(src, NeuralNet):
            src = src.get_all_params()

        target = self.get_all_params()
        src_params = [p.get_value() if hasattr(p, 'get_value') else p
                      for p in src]
        target_params = [p.get_value() for p in target]

        src_shapes = [p.shape for p in src_params]
        target_shapes = [p.shape for p in target_params]
        matches = self._param_alignment(src_shapes, target_shapes)

        for i, j in matches:
            # ii, jj are the indices of the layers, assuming 2
            # parameters per layer
            ii, jj = int(0.5 * i) + 1, int(0.5 * j) + 1
            target[j].set_value(src_params[i])

            if not self.verbose:
                continue
            target_layer_name = list(self.layers_)[jj]
            param_shape = 'x'.join(map(str, src_params[i].shape))
            print("* Loaded parameter from layer {} to layer {} ({}) "
                  "(shape: {})".format(ii, jj, target_layer_name, param_shape))


def plot_loss(net):
    train_loss = [row['train loss'] for row in net.train_history_]
    valid_loss = [row['valid loss'] for row in net.train_history_]
    plt.plot(train_loss, label='train loss')
    plt.plot(valid_loss, label='valid loss')
    plt.legend(loc='best')


def plot_conv_weights(layer, figsize=(6, 6)):
    """Plot the weights of a specific layer. Only really makes sense
    with convolutional layers.

    Parameters
    ----------
    layer : netz.layers.layer
    """
    W = layer.W.get_value()
    shape = W.shape
    nrows = np.ceil(np.sqrt(shape[0])).astype(int)
    ncols = nrows
    for feature_map in range(shape[1]):
        figs, axes = plt.subplots(nrows, ncols, figsize=figsize)
        for ax in axes.flatten():
            ax.set_xticks([])
            ax.set_yticks([])
            ax.axis('off')
        for i, (r, c) in enumerate(it.product(range(nrows), range(ncols))):
            if i >= shape[0]:
                break
            axes[r, c].imshow(W[i, feature_map], cmap='gray',
                              interpolation='nearest')


def plot_conv_activity(layer, x, figsize=(6, 8)):
    """Plot the acitivities of a specific layer. Only really makes
    sense with layers that work 2D data (2D convolutional layers, 2D
    pooling layers ...)

    Parameters
    ----------
    layer : netz.layers.layer

    x : numpy.ndarray
      Only takes one sample at a time, i.e. x.shape[0] == 1.

    """
    if x.shape[0] != 1:
        raise ValueError("Only one sample can be plotted at a time.")
    xs = T.tensor4('xs').astype(theano.config.floatX)
    get_activity = theano.function([xs], layer.get_output(xs))
    activity = get_activity(x)
    shape = activity.shape
    nrows = np.ceil(np.sqrt(shape[1])).astype(int)
    ncols = nrows

    figs, axes = plt.subplots(nrows + 1, ncols, figsize=figsize)
    axes[0, ncols // 2].imshow(1 - x[0][0], cmap='gray',
                               interpolation='nearest')
    axes[0, ncols // 2].set_title('original')
    for ax in axes.flatten():
        ax.set_xticks([])
        ax.set_yticks([])
        ax.axis('off')
    for i, (r, c) in enumerate(it.product(range(nrows), range(ncols))):
        if i >= shape[1]:
            break
        ndim = activity[0][i].ndim
        if ndim != 2:
            raise ValueError("Wrong number of dimensions, image data should "
                             "have 2, instead got {}".format(ndim))
        axes[r + 1, c].imshow(-activity[0][i], cmap='gray',
                              interpolation='nearest')


def occlusion_heatmap(net, x, y, square_length=7):
    """An occlusion test that checks an image for its critical parts.

    In this test, a square part of the image is occluded (i.e. set to
    0) and then the net is tested for its propensity to predict the
    correct label. One should expect that this propensity shrinks of
    critical parts of the image are occluded. If not, this indicates
    overfitting.

    Depending on the depth of the net and the size of the image, this
    function may take awhile to finish, since one prediction for each
    pixel of the image is made.

    Currently, all color channels are occluded at the same time. Also,
    this does not really work if images are randomly distorted.

    See paper: Zeiler, Fergus 2013

    Parameters
    ----------
    net : NeuralNet instance
      The neural net to test.

    x : np.array
      The input data, should be of shape (1, c, x, y). Only makes
      sense with image data.

    y : np.array
      The true value of the image.

    square_length : int (default=7)
      The length of the side of the square that occludes the image.

    Results
    -------
    heat_array : np.array (with same size as image)
      An 2D np.array that at each point (i, j) contains the predicted
      probability of the correct class if the image is occluded by a
      square with center (i, j).

    """
    if (x.ndim != 4) or x.shape[0] != 1:
        raise ValueError("This function requires the input data to be of "
                         "shape (1, c, x, y), instead got {}".format(x.shape))
    img = x[0].copy()
    shape = x.shape
    heat_array = np.zeros(shape[2:])
    pad = square_length // 2
    x_occluded = np.zeros((shape[2] * shape[3], 1, shape[2], shape[3]),
                          dtype=img.dtype)
    for i, j in it.product(*map(range, shape[2:])):
        x_padded = np.pad(img, ((0, 0), (pad, pad), (pad, pad)), 'constant')
        x_padded[:, i:i + square_length, j:j + square_length] = 0.
        x_occluded[i * shape[0] + j, 0] = x_padded[:, pad:-pad, pad:-pad]

    probs = net.predict_proba(x_occluded)
    for i, j in it.product(*map(range, shape[2:])):
        heat_array[i, j] = probs[i * shape[0] + j, y.astype(int)]
    return heat_array


def plot_occlusion(net, X, y, square_length=7, figsize=(9, None)):
    """Plot which parts of an image are particularly import for the
    net to classify the image correctly.

    See paper: Zeiler, Fergus 2013

    Parameters
    ----------
    net : NeuralNet instance
      The neural net to test.

    X : np.array
      The input data, should be of shape (b, c, x, y). Only makes
      sense with image data.

    y : np.array
      The true values of the images.

    square_length : int (default=7)
      The length of the side of the square that occludes the image.

    figsize : tuple (int, int)
      Size of the figure.

    """
    if (X.ndim != 4):
        raise ValueError("This function requires the input data to be of "
                         "shape (b, c, x, y), instead got {}".format(X.shape))
    num_images = X.shape[0]
    if figsize[1] is None:
        figsize = (figsize[0], num_images * figsize[0] / 3)
    figs, axes = plt.subplots(num_images, 3, figsize=figsize)
    for ax in axes.flatten():
        ax.set_xticks([])
        ax.set_yticks([])
        ax.axis('off')
    for n in range(num_images):
        heat_img = occlusion_heatmap(
            net, X[n:n + 1, :, :, :], y[n], square_length
        )

        ax = axes if num_images == 1 else axes[n]
        img = X[n, :, :, :].mean(0)
        ax[0].imshow(-img, interpolation='nearest', cmap='gray')
        ax[0].set_title('image')
        ax[1].imshow(-heat_img, interpolation='nearest', cmap='Reds')
        ax[1].set_title('critical parts')
        ax[2].imshow(-img, interpolation='nearest', cmap='gray')
        ax[2].imshow(-heat_img, interpolation='nearest', cmap='Reds',
                     alpha=0.75)
        ax[2].set_title('super-imposed')
