from __future__ import print_function, division

import numpy as np
from .utils import PeriodicCKDTree, distance_pbc
from tqdm import tqdm
from pymatgen.core.structure import IStructure


class Preprocess(object):
    """
    Preprocess MD trajectoriy data to construct graphs for each frame.

    The input data file is a `NpzFile` generated by `numpy.savez` or
    `numpy.savez_compressed`. For a MD trajectory containing `N` atoms and
    `F` frames, the `NpzFile` should contain the following  keyword variables.

    traj_coords: np.float arrays with shape (F, N, 3), stores the coordinates
        of each atom in each frame.
    lattices: np.float arrays with shape (F, 3, 3), stores the lattice
        matrix of the simulation box in each frame. In the lattice matrix,
        each row represents a lattice vector.
    atom_types: np.int arrays with shape (N,), stores the atomic number of
        each atom in the MD simulation.
    target_index: np.int arrays with shape (n,), stores the indexes of
        the target atoms. (`n <= N`)

    The output data file is stored in a compressed `NpzFile` that contain
    the following keyword variables.

    if backend == 'kdtree'

    traj_coords: np.float32 arrays with shape (F, N, 3), stores the coordinates
        of each atom in each frame.
    lattices: np.float32 arrays with shape (F, 3, 3), stores the lattice
        matrix of the simulation box in each frame. In the lattice matrix,
        each row represents a lattice vector.
    atom_types: np.int32 arrays with shape (N,), stores the atomic number of
        each atom in the MD simulation.
    target_index: np.int32 arrays with shape (n,), stores the indexes of
        the target atoms. (`n <= N`)
    nbr_lists: np.int32 arrays with shape (F, N, n_nbrs), stores the indices
        of the neighboring atoms in the constructed graphs

    elif backend == 'direct'

    traj_coords: np.float32 arrays with shape (F, N, 3), stores the coordinates
        of each atom in each frame.
    atom_types: np.int32 arrays with shape (N,), stores the atomic number of
        each atom in the MD simulation.
    target_index: np.int32 arrays with shape (n,), stores the indexes of
        the target atoms. (`n <= N`)
    nbr_lists: np.int32 arrays with shape (F, N, n_nbrs), stores the indices
        of the neighboring atoms in the constructed graphs
    nbr_dists: np.float32 arrays with shape (F, N, n_nbrs), stores the
        distances between the center atom and the neighboring atoms in the
        constructed graphs

    Parameters
    ----------
    input_file: str, path to the input file
    output_file: str, path to the output file
    n_nbrs: int, number of nearest neighbors to construct the graph
    radius: float, search radius for finding nearest neighbors
    backend: str, either "kdtree" or "direct", the backend used to search for
        nearest neighbors. "kdtree" has linear scaling but only works for
        orthogonal lattices. "direct" works for trigonal lattices but has
        quadratic scaling.
    """
    def __init__(self, input_file, output_file, n_nbrs=20, radius=7.,
                 backend='kdtree', verbose=True):
        self.input_file = input_file
        self.output_file = output_file
        self.n_nbrs = n_nbrs
        self.radius = radius
        if backend not in ['kdtree', 'direct']:
            raise ValueError('backend should be either "kdtree" or "direct", '
                             'but got {}'.format(backend))
        self.backend = backend
        self.verbose = verbose

    def load_data(self):
        with np.load(self.input_file) as f:
            traj_coords = f['traj_coords']
            lattices = f['lattices']
            atom_types = f['atom_types']
            target_index = f['target_index']
        if traj_coords.shape[-1] != 3 or len(traj_coords.shape) != 3:
            raise ValueError('`traj_coords` should have shape (F, N, 3) but '
                             'got {}'.format(traj_coords.shape))
        if lattices.shape[1:] != (3, 3):
            raise ValueError('`lattices` should have shape (F, 3, 3) but '
                             'got {}'.format(lattices.shape))
        if traj_coords.shape[0] != lattices.shape[0]:
            raise ValueError('number of frames in `traj_coords` and `lattice` '
                             'does not match. {} != {}'.format(
                                 traj_coords.shape[0], lattices.shape[0]))
        if traj_coords.shape[1] != atom_types.shape[0]:
            raise ValueError('number of atom in `traj_coords` and `atom_types`'
                             ' does not mathc. {} != {}'.format(
                                 traj_coords.shape[1], atom_types.shape[0]))
        if not set(target_index).issubset(set(np.arange(atom_types.shape[0]))):
            raise ValueError('`target_index` out of boundary')
        for lmat in lattices:
            if not np.allclose(lmat, np.diag(np.diagonal(lmat))):
                if self.backend == 'kdtree':
                    raise ValueError('non-orthogonal lattices can only use '
                                     '"direct" as backend')
        return (traj_coords.astype('float32'),
                lattices.astype('float32'),
                atom_types.astype('int32'),
                target_index.astype('int32'))

    def construct_graph(self, traj_coords, lattices, atom_types, target_index):
        if self.backend == 'kdtree':
            nbr_lists, diag_lattices = [], []
            for coord, lattice in tqdm(zip(traj_coords, lattices),
                                       total=len(traj_coords),
                                       disable=not self.verbose):
                # take the diagonal part of the lattice matrix
                lattice = np.diagonal(lattice)
                diag_lattices.append(lattice)
                pkdt = PeriodicCKDTree(lattice, coord)
                all_nbrs_idxes = pkdt.query_ball_point(coord, self.radius)
                nbr_list = []
                for idx, nbr_idxes in enumerate(all_nbrs_idxes):
                    nbr_dists = distance_pbc(coord[idx],
                                             coord[nbr_idxes],
                                             lattice)
                    nbr_idx_dist = sorted(zip(nbr_idxes, nbr_dists),
                                          key=lambda x: x[1])
                    assert nbr_idx_dist[0][1] == 0 and\
                        nbr_idx_dist[0][0] == idx and\
                        len(nbr_idx_dist) >= self.n_nbrs + 1
                    nbr_list.append([idx for idx, dist in
                                     nbr_idx_dist[1:self.n_nbrs + 1]])
                nbr_lists.append(np.stack(np.array(nbr_list, dtype='int32')))
            nbr_lists = np.stack(nbr_lists)
            diag_lattices = np.stack(diag_lattices)
            return {'traj_coords': traj_coords,
                    'lattices': diag_lattices,
                    'atom_types': atom_types,
                    'target_index': target_index,
                    'nbr_lists': nbr_lists}
        elif self.backend == 'direct':
            nbr_lists, nbr_dists = [], []
            for coord, lattice in tqdm(zip(traj_coords, lattices),
                                       total=len(traj_coords),
                                       disable=not self.verbose):
                crystal = IStructure(lattice=lattice,
                                     species=atom_types,
                                     coords=coord,
                                     coords_are_cartesian=True)
                all_nbrs = crystal.get_all_neighbors(r=self.radius,
                                                     include_index=True)
                all_nbrs = [sorted(nbrs, key=lambda x: x[1])
                            for nbrs in all_nbrs]
                nbr_list, nbr_dist = [], []
                for nbr in all_nbrs:
                    assert len(nbr) >= self.n_nbrs, 'not find enough neighbors'
                    nbr_list.append(list(map(lambda x: x[2],
                                             nbr[:self.n_nbrs])))
                    nbr_dist.append(list(map(lambda x: x[1],
                                             nbr[:self.n_nbrs])))
                nbr_lists.append(np.array(nbr_list, dtype='int32'))
                nbr_dists.append(np.array(nbr_dist, dtype='float32'))
            nbr_lists, nbr_dists = np.stack(nbr_lists), np.stack(nbr_dists)
            return {'traj_coords': traj_coords,
                    'atom_types': atom_types,
                    'target_index': target_index,
                    'nbr_lists': nbr_lists,
                    'nbr_dists': nbr_dists}

    def preprocess(self):
        if self.verbose:
            print('Loading data files')
            print('-' * 80)
        traj_coords, lattices, atom_types, target_index = self.load_data()
        if self.verbose:
            print('Constructing graphs')
            print('-' * 80)
        results = self.construct_graph(traj_coords, lattices, atom_types,
                                       target_index)
        if self.verbose:
            print('Saving results')
            print('-' * 80)
        np.savez_compressed(self.output_file, **results)
        if self.verbose:
            print('Done')