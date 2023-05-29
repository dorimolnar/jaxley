from typing import Dict, List, Optional, Callable
import numpy as np
import jax.numpy as jnp
import pandas as pd

from neurax.modules.base import Module, View
from neurax.modules.branch import Branch, BranchView
from neurax.cell import (
    _compute_num_kids,
    compute_levels,
    compute_branches_in_level,
    _compute_index_of_kid,
    cum_indizes_of_kids,
)
from neurax.stimulus import get_external_input


class Cell(Module):
    cell_params: Dict = {}
    cell_states: Dict = {}

    def __init__(self, branches: List[Branch], parents: List):
        super().__init__()
        self._init_params_and_state(self.cell_params, self.cell_states)
        self._append_to_params_and_state(branches)

        self.branches = branches
        self.nseg = branches[0].nseg
        self.nbranches = len(branches)
        self.parents = jnp.asarray(parents)

        # Indexing.
        self.nodes = pd.DataFrame(
            dict(
                comp_index=np.arange(self.nseg * self.nbranches).tolist(),
                branch_index=(
                    np.arange(self.nseg * self.nbranches) // self.nseg
                ).tolist(),
                cell_index=[0] * (self.nseg * self.nbranches),
            )
        )
        self.coupling_conds_bwd = jnp.asarray([b.coupling_conds_bwd for b in branches])
        self.coupling_conds_fwd = jnp.asarray([b.coupling_conds_fwd for b in branches])
        self.summed_coupling_conds = jnp.asarray(
            [b.summed_coupling_conds for b in branches]
        )
        self.initialized_morph = False
        self.initialized_conds = False

    def __getattr__(self, key):
        assert key == "branch"
        return BranchView(self, self.nodes)

    def init_morph(self):
        self.num_kids = jnp.asarray(_compute_num_kids(self.parents))
        self.levels = compute_levels(self.parents)
        self.branches_in_each_level = compute_branches_in_level(self.levels)

        self.parents_in_each_level = [
            jnp.unique(self.parents[c]) for c in self.branches_in_each_level
        ]

        ind_of_kids = jnp.asarray(_compute_index_of_kid(self.parents))
        ind_of_kids_in_each_level = [
            ind_of_kids[bil] for bil in self.branches_in_each_level
        ]
        self.kid_inds_in_each_level = cum_indizes_of_kids(
            ind_of_kids_in_each_level, max_num_kids=4, reset_at=[0]
        )
        self.initialized_morph = True

    def init_branch_conds(self):
        """Given an axial resisitivity, set the coupling conductances."""
        nbranches = self.nbranches
        nseg = self.nseg

        axial_resistivity = jnp.reshape(
            self.params["axial_resistivity"], (nbranches, nseg)
        )
        radiuses = jnp.reshape(self.params["radius"], (nbranches, nseg))
        lengths = jnp.reshape(self.params["length"], (nbranches, nseg))

        def compute_coupling_cond(rad1, rad2, r_a, l1, l2):
            return rad1 * rad2 ** 2 / r_a / (rad2 ** 2 * l1 + rad1 ** 2 * l2) / l1

        # Compute coupling conductance for segments within a branch.
        # `radius`: um
        # `r_a`: ohm cm
        # `length_single_compartment`: um
        # `coupling_conds`: S * um / cm / um^2 = S / cm / um
        # Compute coupling conductance for segments at branch points.
        rad1 = radiuses[jnp.arange(1, self.nbranches), -1]
        rad2 = radiuses[self.parents[jnp.arange(1, self.nbranches)], 0]
        l1 = lengths[jnp.arange(1, self.nbranches), -1]
        l2 = lengths[self.parents[jnp.arange(1, self.nbranches)], 0]
        r_a1 = axial_resistivity[jnp.arange(1, self.nbranches), -1]
        r_a2 = axial_resistivity[self.parents[jnp.arange(1, self.nbranches)], 0]
        self.branch_conds_bwd = compute_coupling_cond(rad2, rad1, r_a1, l2, l1)
        self.branch_conds_fwd = compute_coupling_cond(rad1, rad2, r_a2, l1, l2)

        # Convert (S / cm / um) -> (mS / cm^2)
        self.branch_conds_fwd *= 10 ** 7
        self.branch_conds_bwd *= 10 ** 7

        for b in range(1, self.nbranches):
            self.summed_coupling_conds = self.summed_coupling_conds.at[b, -1].add(
                self.branch_conds_fwd[b - 1]
            )
            self.summed_coupling_conds = self.summed_coupling_conds.at[
                self.parents[b], 0
            ].add(self.branch_conds_bwd[b - 1])

        self.branch_conds_fwd = jnp.concatenate(
            [jnp.asarray([0.0]), self.branch_conds_fwd]
        )
        self.branch_conds_bwd = jnp.concatenate(
            [jnp.asarray([0.0]), self.branch_conds_bwd]
        )
        self.initialized_conds = True

    def step(
        self,
        u: Dict[str, jnp.ndarray],
        dt: float,
        i_inds: jnp.ndarray,
        i_current: jnp.ndarray,
    ):
        """Step for a single compartment.

        Args:
            u: The full state vector, including states of all channels and the voltage.
            dt: Time step.

        Returns:
            Next state. Same shape as `u`.
        """
        nbranches = self.nbranches
        nseg_per_branch = self.nseg

        if self.branch_conds_fwd is None:
            self.init_branch_conds()

        voltages = u["voltages"]

        # Parameters have to go in here.
        new_channel_states, (v_terms, const_terms) = self.step_channels(
            u, dt, self.branches[0].compartments[0].channels, self.params
        )

        # External input.
        i_ext = get_external_input(
            voltages, i_inds, i_current, self.params["radius"], self.params["length"]
        )

        new_voltages = self.step_voltages(
            voltages=jnp.reshape(voltages, (nbranches, nseg_per_branch)),
            voltage_terms=jnp.reshape(v_terms, (nbranches, nseg_per_branch)),
            constant_terms=jnp.reshape(const_terms, (nbranches, nseg_per_branch))
            + jnp.reshape(i_ext, (nbranches, nseg_per_branch)),
            coupling_conds_bwd=self.coupling_conds_bwd,
            coupling_conds_fwd=self.coupling_conds_fwd,
            summed_coupling_conds=self.summed_coupling_conds,
            branch_cond_fwd=self.branch_conds_fwd,
            branch_cond_bwd=self.branch_conds_bwd,
            num_branches=self.nbranches,
            parents=self.parents,
            kid_inds_in_each_level=self.kid_inds_in_each_level,
            max_num_kids=4,
            parents_in_each_level=self.parents_in_each_level,
            branches_in_each_level=self.branches_in_each_level,
            tridiag_solver="thomas",
            delta_t=dt,
        )
        final_state = new_channel_states[0]
        final_state["voltages"] = new_voltages.flatten(order="C")
        return final_state


class CellView(View):
    def __init__(self, pointer, view):
        super().__init__(pointer, view)

    def __call__(self, index):
        return super().adjust_view("cell_index", index)

    def __getattr__(self, key):
        assert key == "branch"
        return BranchView(self.pointer, self.view)