"""HoloViews-based plotting for labstack.
Contains a set of classes and functions that can be used to plot labstack-style data.

Important Classes:
    - Nodes:
        - Node: a base class for all nodes. Nodes are the basic blocks we use for
            processing data. They can be chained together to form a pipeline.
        - LoaderNode: a node that loads and preprocesses data.
        - ReduxNode: a node that reduces data dimensionality (e.g. by averaging).
        - ValuePlot: plots data values.
        - ComplexHist: plots histograms of complex data ('IQ readout histograms').

    - Widgets:
        - XYSelect: a widget for selecting x and y axes.
"""

from typing import Optional, Union, Any, Dict, Callable
from datetime import datetime

import numpy as np
import pandas as pd
import xarray as xr

import param
from param import Parameter, Parameterized
import panel as pn
from panel.widgets import RadioButtonGroup as RBG

import holoviews as hv
import hvplot.pandas
import hvplot.xarray

import asyncio
import nest_asyncio
nest_asyncio.apply()

import os

from labcore.measurement.sweep import (
    Sweep
)
from labcore.measurement.storage import (
    run_and_save_sweep
)

from pathlib import Path
from labcore.data.datadict_storage import (
    datadict_from_hdf5
)
from labcore.data.datadict import (
    DataDict,
    MeshgridDataDict,
    dd2df,
    datadict_to_meshgrid,
    dd2xr,
)


Data = Union[xr.Dataset, pd.DataFrame]
"""Type alias for valid data. Can be either a pandas DataFrame or an xarray Dataset."""

DataDisplay = Optional[Union[pn.pane.DataFrame, xr.Dataset, str]]
"""Type alias for displaying raw data."""


class Node(pn.viewable.Viewer):
    """Node base class.

    A simple wrapper class that we use to standardize the way we process data.
    Aim: whenever input data is set/updated, ``Node.process`` is called (in the
    base class it simply sets output equal to input).
    User-defined nodes may watch ``data_in`` and ``data_out`` to update UI or
    anything else.
    Pipelines are formed by appending nodes to each other using ``Node.append``.

    Params
    ------
    data_in
        input data. Must be either a pandas DataFrame or an xarray Dataset.
    data_out
        processed output data. Must be either a pandas DataFrame or an xarray Dataset.
    units_in
        units of input data. Must be a dictionary with keys corresponding to dimensions,
        and values corresponding to units.
    units_out
        units of output data. Must be a dictionary with keys corresponding to dimensions,
        and values corresponding to units.
    meta_in
        any input metadata. Arbitrary keys/value.
    meta_out
        any output metadata. Arbitrary keys/value.
    """

    data_in = param.Parameter(None)
    data_out = param.Parameter(None)

    # -- important metadata
    units_in = param.Parameter({})
    units_out = param.Parameter({})
    meta_in = param.Parameter({})
    meta_out = param.Parameter({})

    def __panel__(self) -> pn.viewable.Viewable:
        return self.layout

    def __init__(self, data_in: Optional[Data] = None, *args: Any, **kwargs: Any):
        """Constructor for ``Node``.

        Parameters
        ----------
        data_in
            Optional input data.
        *args:
            passed to ``pn.viewable.Viewer``.
        **kwargs:
            passed to ``pn.viewable.Viewer``.

        """
        self._watchers: Dict[Node, param.parameterized.Watcher] = {}

        super().__init__(*args, **kwargs)
        self.layout = pn.Column()

        # -- options for plotting
        self.plot_type_select = RBG(
            options=["None", "Value", "Readout hist."],
            value="Value",
            name="View as",
        )
        self._plot_obj: Optional[Node] = None

        if data_in is not None:
            self.data_in = data_in
            self.process()

    @staticmethod
    def render_data(data: Optional[Data]) -> DataDisplay:
        """Shows data as renderable object.

        Raises
        ------
        NotImplementedError
            if data is not a pandas DataFrame or an xarray Dataset.
        """
        if data is None:
            return "No data"

        if isinstance(data, pd.DataFrame):
            return pn.pane.DataFrame(data, max_rows=20, show_dimensions=True)
        elif isinstance(data, xr.Dataset):
            return data
        else:
            raise NotImplementedError

    @staticmethod
    def data_dims(data: Optional[Data]) -> tuple[list[str], list[str]]:
        """Returns the dimensions of the data.

        Format: (independents, dependents); both as lists of strings.

        Raises
        ------
        NotImplementedError
            if data is not a pandas DataFrame or an xarray Dataset.
        """
        if data is None:
            return [], []

        if isinstance(data, pd.DataFrame):
            return list(data.index.names), data.columns.to_list()
        elif isinstance(data, xr.Dataset):
            return [str(c) for c in list(data.coords)], list(data.data_vars)
        else:
            raise NotImplementedError

    @staticmethod
    def mean(data: Data, *dims: str) -> Data:
        """Takes the mean of data along the given dimensions.

        Parameters
        ----------
        data
            input data.
        *dims
            dimensions to take the mean along

        Returns
        -------
        data after taking the mean

        Raises
        ------
        NotImplementedError
            if data is not a pandas DataFrame or an xarray Dataset.
        """
        indep, dep = Node.data_dims(data)
        if isinstance(data, pd.DataFrame):
            for d in dims:
                i = indep.index(d)
                indep.pop(i)
            return data.groupby(level=tuple(indep)).mean()
        elif isinstance(data, xr.Dataset):
            for d in dims:
                data = data.mean(d)
            return data
        else:
            raise NotImplementedError

    @staticmethod
    def split_complex(data: Data) -> Data:
        """Split complex dependents into real and imaginary parts.

        TODO: should update units as well

        Parameters
        ----------
        data
            input data.

        Returns
        -------
        data with complex dependents split into real and imaginary parts.

        Raises
        ------
        NotImplementedError
            if data is not a pandas DataFrame or an xarray Dataset.
        """
        indep, dep = Node.data_dims(data)

        if not isinstance(data, pd.DataFrame) and not isinstance(data, xr.Dataset):
            raise NotImplementedError

        dropped = []
        for d in dep:
            if np.iscomplexobj(data[d]):
                data[f"{d}_Re"] = np.real(data[d])
                data[f"{d}_Im"] = np.imag(data[d])
                dropped.append(d)
        if isinstance(data, pd.DataFrame):
            return data.drop(columns=dropped)
        else:
            return data.drop_vars(dropped)

    @staticmethod
    def complex_dependents(data: Optional[Data]) -> dict[str, dict[str, str]]:
        """Returns a dictionary of complex dependents and their real/imaginary parts.

        Requires that complex data has already been split.

        Parameters
        ----------
        data
            input data.

        Returns
        -------
        dictionary of the form:
            "`dependent`": {"real": "`dependent_Re`", "imag": "`dependent_Im`"}}
            `dependent_Re` and `dependent_Im` are the dimensions actually present
            in the data.
        """
        ret = {}
        _, dep = Node.data_dims(data)
        for d in dep:
            if d[-3:] == "_Re":
                im_dep = d[:-3] + "_Im"
                if im_dep in dep:
                    ret[d[:-3]] = dict(real=d, imag=im_dep)
        return ret

    def dim_label(self, dim: str, which: str = "out") -> str:
        """Generate dimension label for use in plots.

        Parameters
        ----------
        dim
            dimension name.
        which
            Either "in" or "out", depending on whether we want the input or
            output data of the Node. Default is "out".

        Returns
        -------
        dimension label, including units if available.
        """
        if which == "out":
            units = self.units_out
        else:
            units = self.units_in

        if dim in units and units[dim] is not None:
            return f"{dim} ({units[dim]})"
        else:
            return f"{dim} (a.u.)"

    def dim_labels(self, which: str = "out") -> dict[str, str]:
        """Generate dimension labels for use in plots.

        Generates all dimension labels for the data.
        See ``Node.dim_label`` for more information.
        """
        if which == "out":
            indep, dep = self.data_dims(self.data_out)
        else:
            indep, dep = self.data_dims(self.data_in)
        dims = indep + dep
        return {d: self.dim_label(d, which=which) for d in dims}

    def update(self, *events: param.parameterized.Event) -> None:
        """Update the node using external events.

        If event contains ``data_out``, ``units_out``, or ``meta_out``,
        will set them as ``data_in``, ``units_in``, or ``meta_in`` respectively.
        """
        for e in events:
            if e.name == "data_out":
                self.data_in = e.new
            elif e.name == "units_out":
                self.units_in = e.new
            elif e.name == "meta_out":
                self.meta_in = e.new

    @pn.depends("data_in", watch=True)
    def process(self) -> None:
        """Process data.

        By default, simply sets ``data_out`` equal to ``data_in``.

        Can/Should be overridden by subclasses to do more complicated things.
        """
        self.data_out = self.data_in

    @pn.depends("data_in")
    def data_in_view(self) -> DataDisplay:
        """Updating view of input data (as table; as provided by the data type).

        Updates on change of ``data_in``.
        """
        return self.render_data(self.data_in)

    @pn.depends("data_out")
    def data_out_view(self) -> DataDisplay:
        """Updating view of output data (as table; as provided by the data type).

        Updates on change of ``data_out``.
        """
        return self.render_data(self.data_out)

    @pn.depends("data_out")
    def plot(self) -> pn.viewable.Viewable:
        """A reactive panel object that allows selecting a plot type, and shows the plot.

        Updates on change of ``data_out``.
        """
        return pn.Column(
            labeled_widget(self.plot_type_select),
            self.plot_obj,
        )

    @pn.depends("data_out", "plot_type_select.value")
    def plot_obj(self) -> Optional["Node"]:
        """The actual plot object.

        Updates on change of ``data_out`` or the selection of the plot value.

        Returns
        -------
        A dedicated plotting node.
        """
        if self.plot_type_select.value == "Value":
            if not isinstance(self._plot_obj, ValuePlot):
                if self._plot_obj is not None:
                    self.detach(self._plot_obj)
                self._plot_obj = ValuePlot(name="plot")
                self.append(self._plot_obj)
                self._plot_obj.data_in = self.data_out

        elif self.plot_type_select.value == "Readout hist.":
            if not isinstance(self._plot_obj, ComplexHist):
                if self._plot_obj is not None:
                    self.detach(self._plot_obj)
                self._plot_obj = ComplexHist(name="plot")
                self.append(self._plot_obj)
                self._plot_obj.data_in = self.data_out

        else:
            if self._plot_obj is not None:
                self.detach(self._plot_obj)
            self._plot_obj = self.data_out_view

        return self._plot_obj

    def append(self, other: "Node") -> None:
        watcher = self.param.watch(other.update, ["data_out", "units_out", "meta_out"])
        self._watchers[other] = watcher

    def detach(self, other: "Node") -> None:
        if other in self._watchers:
            self.param.unwatch(self._watchers[other])
            del self._watchers[other]


class LoaderNodeBase(Node):
    """A node that loads data.

    the panel of the node consists of UI options for loading and pre-processing.

    Each subclass must implement ``LoaderNodeBase.load_data``.
    """

    def __init__(self, *args: Any, **kwargs: Any):
        """Constructor for ``LoaderNode``.

        Parameters
        ----------
        *args:
            passed to ``Node``.
        **kwargs:
            passed to ``Node``.
        """
        super().__init__(*args, **kwargs)

        self.pre_process_opts = RBG(
            options=[None, "Average"], value="Average", name="Pre-processing"
        )
        self.pre_process_dim_input = pn.widgets.TextInput(
            value="repetition", name="Pre-process dimension"
        )
        self.grid_on_load_toggle = pn.widgets.Checkbox(value=True, name="Auto-grid")

        self.layout = pn.Column(
            pn.Row(labeled_widget(self.pre_process_opts), self.pre_process_dim_input),
            self.grid_on_load_toggle,
        )

        self.generate_button = pn.widgets.Button(name="Load data")
        self.generate_button.on_click(self.load_and_preprocess)
        self.layout.append(self.generate_button)

    def load_and_preprocess(self, *events: param.parameterized.Event) -> None:
        """Call load data and perform pre-processing.

        Function is triggered by clicking the "Load data" button.
        """
        dd = self.load_data()  # this is simply a datadict now.

        # this is the case for making a pandas DataFrame
        if not self.grid_on_load_toggle.value:
            data = self.split_complex(dd2df(dd))
            indep, dep = self.data_dims(data)

            if self.pre_process_dim_input.value in indep:
                if self.pre_process_opts.value == "Average":
                    data = self.mean(data, self.pre_process_dim_input.value)
                    indep.pop(indep.index(self.pre_process_dim_input.value))

        # when making gridded data, can do things slightly differently
        # TODO: what if gridding goes wrong?
        else:
            mdd = datadict_to_meshgrid(dd)

            if self.pre_process_dim_input.value in mdd.axes():
                if self.pre_process_opts.value == "Average":
                    mdd = mdd.mean(self.pre_process_dim_input.value)

            data = self.split_complex(dd2xr(mdd))
            indep, dep = self.data_dims(data)

        for dim in indep + dep:
            self.units_out[dim] = dd.get(dim, {}).get("unit", None)

        self.data_out = data

    def load_data(self) -> DataDict:
        """Load data. Needs to be implemented by subclasses.
        
        Raises
        ------
        NotImplementedError
            if not implemented by subclass.
        """
        raise NotImplementedError


class LoaderNodeSweep(LoaderNodeBase):
    """A node that performs a predeclared sweep then plots from the saved file location

    the panel of the node consists of UI options for loading and pre-processing.
    """
    
    def __init__(self, input_sweep:Sweep = Sweep(None), name:str = "", sweep_kwargs:dict= {}, sweep_func: Optional[Callable] = None, *args: Any, **kwargs: Any):
        """Constructor for ``LoaderNodeSweep``.

        Parameters
        ----------
        input_sweep:
            Sweep to be executed
        name:
            name of save file
        sweep_kwargs:
            **kwargs to be passed to the Sweep when executed (as a dict)
        sweep_func:
            Function used to call the sweep
        *args:
            passed to ``Node``.
        **kwargs:
            passed to ``Node``.
        """
        super().__init__(*args, **kwargs)
        self.file_loc = ""
        self.file_name = pn.widgets.TextInput(
            name="File Name", value = name
        )
        self.sweep_button = pn.widgets.Button(name="Perform Sweep")
        self.sweep_button.on_click(lambda event, arg1 = 'DefaultArg': self.trigger_perform_sweep_button(name, input_sweep, sweep_func, **sweep_kwargs))
        self.layout = pn.Column(
            pn.Row(labeled_widget(self.pre_process_opts), self.pre_process_dim_input),
            self.file_name,
            self.sweep_button,
            self.grid_on_load_toggle,
        )
        self.generate_button = pn.widgets.Button(name="Load data")
        self.generate_button.on_click(self.trigger_load_data_button)
        self.layout.append(self.generate_button)

    def trigger_perform_sweep_button(self, name: str, input_sweep: Sweep, sweep_func:Optional[Callable], *events: param.parameterized.Event,**kwargs: Any) -> None:
        """
        Runs and saves sweep, also saves file location

        Triggered when the 'Perform Sweep' button is pressed

        """
        if sweep_func is None:
            path_loc = run_and_save_sweep(input_sweep,os.path.join(os.getcwd(), 'data'), name, save_action_kwargs = True, **kwargs)
        else:
            path_loc = sweep_func(input_sweep,name,**kwargs)
        sweep_path = os.path.abspath(path_loc[0]) + "\data.ddh5"
        sweep_path = sweep_path.replace("C:","")
        self.file_loc = str(sweep_path)
    
    def load_data(self,*events: param.parameterized.Event) -> None:
        """
        Load data from the file location specified
        """
        return datadict_from_hdf5(self.file_loc)
        

    def trigger_load_data_button(self, *events: param.parameterized.Event) -> None:
        """
        Triggered when the 'Load Data' button is pressed
        """
        self.load_and_preprocess()
        

class LoaderNodePath(LoaderNodeBase):
    """A node that loads data from a specified file location.

    the panel of the node consists of UI options for loading and pre-processing.

    """

    def __init__(self, path:str = '', *args: Any, **kwargs: Any):
        """Constructor for ``LoaderNodePath``.

        Parameters
        ----------
        path:
            python path of file to load
        *args:
            passed to ``Node``.
        **kwargs:
            passed to ``Node``.
        """
        super().__init__(*args, **kwargs)
        self.file_loc = pn.widgets.TextInput(
            name="File Location", value = path
        )
        self.file_loc.param.trigger('value')
        self.refresh_rate =pn.widgets.FloatSlider(
            name='Refresh Rate (Seconds)', start=1, end=10, step=1
            )
        self.pause_refresh = pn.widgets.Toggle(name="Pause Refresh")
        self.layout = pn.Column(
            pn.Row(labeled_widget(self.pre_process_opts), self.pre_process_dim_input),
            pn.Row(self.refresh_rate,
            self.pause_refresh),
            self.file_loc,
            self.grid_on_load_toggle,
        )

        self.generate_button = pn.widgets.Button(name="Load data")
        self.generate_button.on_click(self.trigger_load_data_button)
        self.layout.append(self.generate_button)

    async def update_data(self):
        """
        Async function to automatically refresh the data
        """
        while (True):
            await asyncio.sleep(self.refresh_rate.value)
            if not self.pause_refresh.value:
                self.load_and_preprocess()

    def trigger_load_data_button(self, *events: param.parameterized.Event) -> None:
        """
        Triggered when the 'Load Data' button is pressed
        """
        self.load_and_preprocess()
        self.task = asyncio.ensure_future(self.update_data())
        
    def load_data(self) -> DataDict:
        """
        Load data from the file location specified
        """
        return datadict_from_hdf5(self.file_loc.value)


class ReduxNode(Node):
    OPTS = ["None", "Mean"]

    coordinates = param.List(default=[])
    operations = param.List(default=[])

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._widgets = {}
        self.layout = pn.Column()

    def __panel__(self):
        return self.layout

    @pn.depends("data_in", watch=True)
    def on_input_update(self):
        self.coords = list(self.data_in.dims.keys())
        for c in self.coords:
            if c not in self._widgets:
                w = RBG(name=c, options=self.OPTS, value=self.OPTS[0])
                ui = pn.Row(f"{c}", w)
                self.layout.append(ui)
                self._widgets[c] = {
                    "widget": w,
                    "ui": ui,
                    "change_cb": w.param.watch(self.on_widget_change, ["value"]),
                }

        for c in list(self._widgets.keys()):
            if c not in self.coords:
                self.layout.remove(self._widgets[c]["ui"])
                del self._widgets[c]

        self.on_widget_change()

    @pn.depends("operations")
    def on_operations_change(self):
        for c, o in zip(self.coords, self.operations):
            self._widgets[c].value = o

    def on_widget_change(self, *events):
        self.operations = [self._widgets[c]["widget"].value for c in self.coords]

    @pn.depends("data_in", "operations", watch=True)
    def process(self):
        out = self.data_in
        for c, o in zip(self.coords, self.operations):
            if o == "Mean":
                out = out.mean(c)
        self.data_out = out


class XYSelect(pn.viewable.Viewer):
    value = param.Tuple(default=("None", "None"))
    options = param.List(
        default=[
            "None",
        ]
    )

    def __init__(self):
        self._xrbg = RBG(options=self.options, name="x")
        self._yrbg = RBG(options=self.options, name="y")
        super().__init__()
        self._layout = pn.Column(
            labeled_widget(self._xrbg),
            labeled_widget(self._yrbg),
        )

        self._sync_x()
        self._sync_y()

    def __panel__(self):
        return self._layout

    @param.depends("options", watch=True)
    def on_option_change(self):
        self._xrbg.options = self.options
        self._yrbg.options = self.options

    @param.depends("value", watch=True)
    def _sync_widgets(self):
        if self.value[0] == self.value[1] and self.value[0] != "None":
            self.value = self.value[0], "None"
        self._xrbg.name = self.name
        self._xrbg.value = self.value[0]
        self._yrbg.value = self.value[1]

    @param.depends("_xrbg.value", watch=True)
    def _sync_x(self):
        x = self._xrbg.value
        y = self.value[1]
        if y == x:
            y = "None"
        self.value = (x, y)

    @param.depends("_yrbg.value", watch=True)
    def _sync_y(self):
        y = self._yrbg.value
        x = self.value[0]
        if y == x:
            x = "None"
        self.value = (x, y)


# -- generic plot functions


class ValuePlot(Node):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.xy_select = XYSelect()
        self._old_indep = []

    def __panel__(self):
        return pn.Column(
            self.plot_options_panel,
            self.plot_panel,
        )

    @pn.depends("data_out")
    def plot_options_panel(self):
        indep, dep = self.data_dims(self.data_out)

        opts = ["None"]
        if indep is not None:
            opts += indep
        self.xy_select.options = opts

        if indep != self._old_indep:
            if len(opts) > 2:
                self.xy_select.value = (opts[-2], opts[-1])
            elif len(opts) > 1:
                self.xy_select.value = (opts[-1], "None")
        self._old_indep = indep

        return self.xy_select

    @pn.depends("data_out", "xy_select.value")
    def plot_panel(self):
        plot = "*No valid options chosen.*"
        x, y = self.xy_select.value
        indep, dep = self.data_dims(self.data_out)

        if x in ["None", None]:
            pass

        # case: a line or scatter plot (or multiple of these)
        elif y in ["None", None]:
            if isinstance(self.data_out, pd.DataFrame):
                plot = self.data_out.hvplot.line(
                    x=x, xlabel=self.dim_label(x)
                ) * self.data_out.hvplot.scatter(x=x)

            elif isinstance(self.data_out, xr.Dataset):
                plot = self.data_out.hvplot.line(
                    x=x,
                    xlabel=self.dim_label(x),
                ) * self.data_out.hvplot.scatter(x=x)
            else:
                raise NotImplementedError

        # case: if x and y are selected, we make a 2d plot of some sort
        else:
            if isinstance(self.data_out, pd.DataFrame):
                plot = plot_df_as_2d(self.data_out, x, y, dim_labels=self.dim_labels())
            elif isinstance(self.data_out, xr.Dataset):
                plot = plot_xr_as_2d(self.data_out, x, y, dim_labels=self.dim_labels())
            else:
                raise NotImplementedError

        return plot


class ComplexHist(Node):
    def __init__(self, *args, **kwargs):
        self.gb_select = pn.widgets.CheckButtonGroup(
            name="Group by",
            options=[],
            value=[],
        )
        super().__init__(*args, **kwargs)

        self.layout = pn.Column(
            labeled_widget(self.gb_select),
            self.plot_panel,
        )

    def __panel__(self):
        return self.layout

    @pn.depends("data_out", watch=True)
    def _sync_options(self):
        indep, dep = self.data_dims(self.data_out)
        if isinstance(indep, list):
            self.gb_select.options = indep

    @pn.depends("data_out", "gb_select.value")
    def plot_panel(self):
        plot = "*No valid options chosen.*"

        layout = pn.Column()
        for k, v in self.complex_dependents(self.data_out).items():
            xlim = float(self.data_out[v["real"]].min()), float(
                self.data_out[v["real"]].max()
            )
            ylim = float(self.data_out[v["imag"]].min()), float(
                self.data_out[v["imag"]].max()
            )
            p = self.data_out.hvplot(
                kind="hexbin",
                aspect=1,
                groupby=self.gb_select.value,
                x=v["real"],
                y=v["imag"],
                xlim=xlim,
                ylim=ylim,
                clabel="count",
            )
            layout.append(p)
            plot = layout

        return plot


def plot_df_as_2d(df, x, y, dim_labels={}):
    indeps, deps = Node.data_dims(df)

    if x in indeps and y in indeps:
        return pn.Column(
            *[
                df.hvplot.heatmap(
                    x=x,
                    y=y,
                    C=d,
                    xlabel=dim_labels.get(x, x),
                    ylabel=dim_labels.get(y, y),
                    clabel=f"Mean {dim_labels.get(d, d)}",
                ).aggregate(function=np.mean)
                for d in deps
            ]
        )
    elif x in deps + indeps and y in deps:
        return df.hvplot.scatter(
            x=x,
            y=y,
            xlabel=dim_labels.get(x, x),
            ylabel=dim_labels.get(y, y),
        )
    else:
        return "*that's currently not supported :(*"


def plot_xr_as_2d(ds, x, y, dim_labels={}):
    if ds is None:
        return "Nothing to plot."

    indeps, deps = Node.data_dims(ds)
    plot = None

    # plotting stuff vs two independent -- heatmap
    if x in indeps and y in indeps:
        for d in deps:
            if plot is None:
                plot = ds.get(d).hvplot.quadmesh(
                    x=x,
                    y=y,
                    xlabel=dim_labels.get(x, x),
                    ylabel=dim_labels.get(y, y),
                    clabel=f"Mean {dim_labels.get(d, d)}",
                )
            else:
                plot += ds.get(d).hvplot.quadmesh(
                    x=x,
                    y=y,
                    xlabel=dim_labels.get(x, x),
                    ylabel=dim_labels.get(y, y),
                    clabel=f"Mean {dim_labels.get(d, d)}",
                )
        return plot.cols(1)

    else:
        return "*Not a valid plot*"


# -- specific plot functions


# -- various tool functions


def labeled_widget(w, lbl=None):
    m = w.margin

    if lbl is None:
        lbl = w.name

    lbl_w = pn.widgets.StaticText(value=lbl, margin=(m[0], m[1], 0, m[1]))
    w.margin = (0, m[1], m[0], m[1])
    return pn.Column(
        lbl_w,
        w,
    )

# -- convenience functions

def plot_data(data: Union[pd.DataFrame, xr.Dataset]) -> pn.viewable.Viewable:
    n = Node(data, name='plot')
    return pn.Column(
        n,
        n.plot,
    )
