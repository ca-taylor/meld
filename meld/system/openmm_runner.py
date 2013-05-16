from simtk.openmm.app import AmberPrmtopFile, OBC2, GBn, GBn2, Simulation
from simtk.openmm.app import forcefield as ff
from simtk.openmm import LangevinIntegrator, MeldForce
from simtk.unit import kelvin, picosecond, femtosecond, angstrom
from simtk.unit import Quantity, kilojoule, mole
from .restraints import SelectableRestraint, NonSelectableRestraint, DistanceRestraint, TorsionRestraint


gas_constant = 8.314e-3


class OpenMMRunner(object):
    def __init__(self, system, options):
        if system.temperature_scaler is None:
            raise RuntimeError('system does not have temparture_scaler set')
        else:
            self.temperature_scaler = system.temperature_scaler
        self._parm_string = system.top_string
        self._always_on_restraints = system.restraints.always_active
        self._selectable_collections = system.restraints.selectively_active_collections
        self._options = options
        self._simulation = None
        self._alpha = 0.
        self._temperature = None

    def set_alpha(self, alpha):
        self._alpha = alpha
        self._temperature = self.temperature_scaler(alpha)
        self._initialize_simulation()

    def minimize_then_run(self, state):
        return self._run(state, minimize=True)

    def run(self, state):
        return self._run(state, minimize=False)

    def get_energy(self, state):
        # set the coordinates
        coordinates = Quantity(state.positions, angstrom)
        self._simulation.context.setPositions(coordinates)

        # get the energy
        snapshot = self._simulation.context.getState(getPositions=True, getVelocities=True, getEnergy=True)
        e_potential = snapshot.getPotentialEnergy()
        e_potential = e_potential.value_in_unit(kilojoule / mole) / gas_constant / self._temperature

        return e_potential

    def _initialize_simulation(self):
        prmtop = _parm_top_from_string(self._parm_string)
        sys = _create_openmm_system(prmtop, self._options.cutoff, self._options.use_big_timestep,
                                    self._options.implicit_solvent_model)

        meld_rests = _add_always_active_restraints(sys, self._always_on_restraints, self._alpha)
        _add_selectively_active_restraints(sys, self._selectable_collections, meld_rests, self._alpha)

        integrator = _create_integrator(self._temperature, self._options.use_big_timestep)
        self._simulation = _create_openmm_simulation(prmtop.topology, sys, integrator)

    def _run(self, state, minimize):
        assert state.alpha == self._alpha

        # add units to coordinates and velocities (we store in Angstrom, openmm
        # uses nm
        coordinates = Quantity(state.positions, angstrom)
        velocities = Quantity(state.velocities, angstrom / picosecond)

        # set the positions
        self._simulation.context.setPositions(coordinates)

        # run energy minimization
        if minimize:
            self._simulation.minimizeEnergy(self._options.minimize_steps)

        # set the velocities
        self._simulation.context.setVelocities(velocities)

        # run timesteps

        self._simulation.step(self._options.timesteps)

        # extract coords, vels, energy and strip units
        snapshot = self._simulation.context.getState(getPositions=True, getVelocities=True, getEnergy=True)
        coordinates = snapshot.getPositions(asNumpy=True).value_in_unit(angstrom)
        velocities = snapshot.getVelocities(asNumpy=True).value_in_unit(angstrom / picosecond)
        e_potential = snapshot.getPotentialEnergy().value_in_unit(kilojoule / mole) / gas_constant / self._temperature

        # store in state
        state.positions = coordinates
        state.velocities = velocities
        state.energy = e_potential

        return state


def _create_openmm_simulation(topology, system, integrator):
    return Simulation(topology, system, integrator)


def _parm_top_from_string(parm_string):
    return AmberPrmtopFile(parm_string=parm_string)


def _create_openmm_system(parm_object, cutoff, use_big_timestep, implicit_solvent):
    if cutoff is None:
        cutoff_type = ff.NoCutoff
        cutoff_dist = 999.
    else:
        cutoff_type = ff.CutoffNonPeriodic
        cutoff_dist = cutoff

    if use_big_timestep:
        constraint_type = ff.HAngles
    else:
        constraint_type = ff.HBonds

    if implicit_solvent == 'obc':
        implicit_type = OBC2
    elif implicit_solvent == 'gbNeck':
        implicit_type = GBn
    elif implicit_solvent == 'gbNeck2':
        implicit_type = GBn2
    return parm_object.createSystem(nonbondedMethod=cutoff_type, nonbondedCutoff=cutoff_dist,
                                    constraints=constraint_type, implicitSolvent=implicit_type)


def _create_integrator(temperature, use_big_timestep):
    if use_big_timestep:
        timestep = 3.5 * femtosecond
    else:
        timestep = 2.0 * femtosecond
    return LangevinIntegrator(temperature * kelvin, 1.0 / picosecond, timestep)


def _add_always_active_restraints(system, restraint_list, alpha):
    selectable_restraints = []
    nonselectable_restraints = []
    for rest in restraint_list:
        if isinstance(rest, SelectableRestraint):
            selectable_restraints.append(rest)
        elif isinstance(rest, NonSelectableRestraint):
            nonselectable_restraints.append(rest)
        else:
            raise RuntimeError('Unknown type of restraint {}'.format(rest))
    if nonselectable_restraints:
        raise NotImplementedError('Non-meld restraints are not implemented yet')
    return selectable_restraints


def _add_selectively_active_restraints(system, collections, always_on, alpha):
    if not (collections or always_on):
        # we don't need to do anything
        return
    # otherwise we need a MeldForce
    meld_force = MeldForce()

    if always_on:
        group_list = []
        for rest in always_on:
            rest_index = _add_meld_restraint(rest, meld_force, alpha)
            group_index = meld_force.addGroup([rest_index], 1)
            group_list.append(group_index)
        meld_force.addCollection(group_list, len(group_list))
    for coll in collections:
        group_indices = []
        for group in coll.groups:
            restraint_indices = []
            for rest in group.restraints:
                rest_index = _add_meld_restraint(rest, meld_force, alpha)
                restraint_indices.append(rest_index)
            group_index = meld_force.addGroup(restraint_indices, group.num_active)
            group_indices.append(group_index)
        meld_force.addCollection(group_indices, coll.num_active)
    system.addForce(meld_force)


def _add_meld_restraint(rest, meld_force, alpha):
    scale = rest.scaler(alpha)
    if isinstance(rest, DistanceRestraint):
        rest_index = meld_force.addDistanceRestraint(rest.atom_index_1, rest.atom_index_2,
                                                    rest.r1, rest.r2, rest.r3, rest.r4,
                                                    rest.k * scale)
    elif isinstance(rest, TorsionRestraint):
        rest_index = meld_force.addTorsionRestraint(rest.atom_index_1, rest.atom_index_2,
                                                    rest.atom_index_3, rest.atom_index_4,
                                                    rest.phi, rest.delta_phi, rest.k * scale)
    else:
        raise RuntimeError('Do not know how to handle restraint {}'.format(rest))
    return rest_index
