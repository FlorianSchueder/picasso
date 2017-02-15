"""
    gui/average
    ~~~~~~~~~~~~~~~~~~~~

    Graphical user interface for averaging particles

    :author: Joerg Schnitzbauer, 2015
    :author 3d averaging: Maximilian Strauss, 2016
    :copyright: Copyright (c) 2016 Jungmann Lab, Max Planck Institute of Biochemistry
"""
import functools
import multiprocessing
import os.path
import sys
import time
import traceback
from multiprocessing import sharedctypes

import matplotlib.pyplot as plt
import numba
import numpy as np
import scipy
from PyQt4 import QtCore, QtGui

from mpl_toolkits.mplot3d import Axes3D
from numpy.lib.recfunctions import stack_arrays

from .. import io, lib, render



@numba.jit(nopython=True, nogil=True)
def render_hist(x, y, oversampling, t_min, t_max):
    n_pixel = int(np.ceil(oversampling * (t_max - t_min)))
    in_view = (x > t_min) & (y > t_min) & (x < t_max) & (y < t_max)
    x = x[in_view]
    y = y[in_view]
    x = oversampling * (x - t_min)
    y = oversampling * (y - t_min)
    image = np.zeros((n_pixel, n_pixel), dtype=np.float32)
    render._fill(image, x, y)
    return len(x), image

def render_histz(x, z, oversampling, x_min, x_max, z_min, z_max, pixelsize):
    n_pixelx = int(np.ceil(oversampling * (x_max - x_min)))
    n_pixel_z = int(np.ceil(oversampling * (z_max - z_min)/pixelsize))
    in_view = (x > x_min) & (z > z_min) & (x < x_max) & (z < z_max)
    x = x[in_view]
    z = z[in_view]
    x = oversampling * (x - x_min)
    z = oversampling * (z - z_min)/pixelsize
    image = np.zeros((n_pixelx, n_pixel_z), dtype=np.float32)
    render._fill(image, z, x)
    return len(x), image

@numba.jit(nopython=True, nogil=True)
def render_hist3d(x, y, z, oversampling, t_min, t_max, z_min, z_max, pixelsize):
    n_pixel = int(np.ceil(oversampling * (t_max - t_min)))
    n_pixel_z = int(np.ceil(oversampling * (z_max - z_min)/pixelsize))
    in_view = (x > t_min) & (y > t_min) & (z > z_min) & (x < t_max) & (y < t_max) & (z < z_max)
    x = x[in_view]
    y = y[in_view]
    z = z[in_view]
    x = oversampling * (x - t_min)
    y = oversampling * (y - t_min)
    z = oversampling * (z - z_min)/pixelsize
    image = np.zeros((n_pixel, n_pixel, n_pixel_z), dtype=np.float32)
    render._fill3d(image, x, y, z)
    return len(x), image

def compute_xcorr(CF_image_avg, image):
    F_image = np.fft.fft2(image)
    xcorr = np.fft.fftshift(np.real(np.fft.ifft2((F_image * CF_image_avg))))
    return xcorr

def integralImage(A,szT):
    szA = A.shape
    B = np.zeros(szA+np.multiply(szT,2)-1)
    B[szT[0]:szT[0]+szA[0], szT[1]:szT[1]+szA[1], szT[2]:szT[2]+szA[2] ] = A; # add A to the End
    s = np.cumsum(B,0);
    c = s[szT[0]:,:,:]-s[0:-szT[0],:,:];
    s = np.cumsum(c,1);
    c = s[:,szT[1]:,:]-s[:,0:-szT[1],:];
    s = np.cumsum(c,2);
    integralImageA = s[:,:,szT[2]:]-s[:,:,0:-szT[2]];
    return integralImageA


def compute_xcorr3(T,A):
    intImgA = integralImage(A,T.shape)
    intImgA2 = integralImage(np.multiply(A,A),T.shape)

    rotT = np.flipud(np.fliplr(T[:,:,::-1]))
    fftRotT  = np.fft.fftn(rotT,intImgA.shape)
    fftA = np.fft.fftn(A,intImgA.shape)

    C = np.real(np.fft.ifftn(np.multiply(fftA,fftRotT)))

    shiftco = (np.subtract(np.subtract(A.shape,1),(np.unravel_index(C.argmax(),C.shape))))
    shiftco = [shiftco[1],shiftco[0],shiftco[2]]
    maxval = C.argmax()
    sumval = np.sum(C)

    return shiftco, maxval, sumval


def align_group(angles, oversampling, t_min, t_max, CF_image_avg, image_half, counter, lock, group):
    with lock:
        counter.value += 1
    index = group_index[group].nonzero()[1]
    x_rot = x[index]
    y_rot = y[index]
    x_original = x_rot.copy()
    y_original = y_rot.copy()
    xcorr_max = 0.0
    for angle in angles:
        # rotate locs
        x_rot = np.cos(angle) * x_original - np.sin(angle) * y_original
        y_rot = np.sin(angle) * x_original + np.cos(angle) * y_original
        # render group image
        N, image = render_hist(x_rot, y_rot, oversampling, t_min, t_max)
        # calculate cross-correlation
        xcorr = compute_xcorr(CF_image_avg, image)
        # find the brightest pixel
        y_max, x_max = np.unravel_index(xcorr.argmax(), xcorr.shape)
        # store the transformation if the correlation is larger than before
        if xcorr[y_max, x_max] > xcorr_max:
            xcorr_max = xcorr[y_max, x_max]
            rot = angle
            dy = np.ceil(y_max - image_half) / oversampling
            dx = np.ceil(x_max - image_half) / oversampling
    # rotate and shift image group locs
    x[index] = np.cos(rot) * x_original - np.sin(rot) * y_original - dx
    y[index] = np.sin(rot) * x_original + np.cos(rot) * y_original - dy


def align_group3d(angles, oversampling, t_min, t_max, z_min, z_max, CF_image_avg3, CF_image_avg, image_half, pixelsize, counter, lock, group):
    with lock:
        counter.value += 1
    index = group_index[group].nonzero()[1]
    x_rot = x[index]
    y_rot = y[index]
    x_original = x_rot.copy()
    y_original = y_rot.copy()
    xcorr_max = 0.0
    for angle in angles:
        # rotate locs
        x_rot = np.cos(angle) * x_original - np.sin(angle) * y_original
        y_rot = np.sin(angle) * x_original + np.cos(angle) * y_original
        # render group image
        N, image = render_hist(x_rot, y_rot, oversampling, t_min, t_max)
        # calculate cross-correlation
        xcorr = compute_xcorr(CF_image_avg, image)
        # find the brightest pixel
        y_max, x_max = np.unravel_index(xcorr.argmax(), xcorr.shape)
        # store the transformation if the correlation is larger than before
        if xcorr[y_max, x_max] > xcorr_max:
            xcorr_max = xcorr[y_max, x_max]
            rot = angle
            dy = np.ceil(y_max - image_half) / oversampling
            dx = np.ceil(x_max - image_half) / oversampling
    # rotate and shift image group locs
    x[index] = np.cos(rot) * x_original - np.sin(rot) * y_original - dx
    y[index] = np.sin(rot) * x_original + np.cos(rot) * y_original - dy

def align_group3d_2(angles, oversampling, t_min, t_max, z_min, z_max, CF_image_avg3, CF_image_avg, image_half, pixelsize, counter, lock, group):
    with lock:
        counter.value += 1
    index = group_index[group].nonzero()[1]
    x_rot = x[index]
    z_rot = z[index]
    z_original = z_rot.copy()
    x_original = x_rot.copy()

    N, image = render_histz(x_rot, z_rot, oversampling, t_min, t_max, z_min, z_max, pixelsize)
    #fig = plt.figure()
    #fig.canvas.set_window_title('image xz')
    #ax1 = fig.add_subplot(121)
    #ax2 = fig.add_subplot(122)
    #ax1.imshow(image)
    #ax2.imshow(CF_image_avg)
    plt.show()
    xcorr = compute_xcorr(CF_image_avg, image)
    # find the brightest pixel
    xpos_max, zpos_max = np.unravel_index(xcorr.argmax(), xcorr.shape)

    dz = np.ceil(zpos_max - image_half) / oversampling #Check pixelsize argument again
    dx = np.ceil(xpos_max - image_half) / oversampling #Check pixelsize argument again
    print('---dz---')
    print(dz)

    z[index] = z_original + dz
    #x[index] = x_original + dx

def init_pool(x_, y_, group_index_):
    global x, y, group_index
    x = np.ctypeslib.as_array(x_)
    y = np.ctypeslib.as_array(y_)
    group_index = group_index_

def init_pool3(x_, y_, z_, group_index_):
    global x, y, z, group_index
    x = np.ctypeslib.as_array(x_)
    y = np.ctypeslib.as_array(y_)
    z = np.ctypeslib.as_array(z_)
    group_index = group_index_


class Worker(QtCore.QThread):

    progressMade = QtCore.pyqtSignal(int, int, int, int, np.recarray, bool)

    def __init__(self, locs, r, group_index, oversampling, iterations, pixelsize, r_z):
        super().__init__()
        self.locs = locs.copy()
        self.r = r
        self.t_min = -r
        self.t_max = r
        self.z_min = -r_z
        self.z_max = r_z
        self.group_index = group_index
        self.oversampling = oversampling
        self.iterations = iterations
        self.pixelsize = pixelsize
        print('Z vlaues')
        print(self.z_min)
        print(self.z_max)

    def run(self):
        has_z = hasattr(self.locs[0], 'z')
        if has_z:
            print('Running in 3D')
            n_groups = self.group_index.shape[0]
            a_step = np.arcsin(1 / (self.oversampling * self.r))
            angles = np.arange(0, 2*np.pi, a_step)
            n_workers = max(1, int(0.75 * multiprocessing.cpu_count()))
            manager = multiprocessing.Manager()
            counter = manager.Value('d', 0)
            lock = manager.Lock()
            groups_per_worker = max(1, int(n_groups / n_workers))
            for it in range(self.iterations):
                counter.value = 0
                # render average image
                N_avg, image_avg = render.render_hist(self.locs, self.oversampling, self.t_min, self.t_min, self.t_max, self.t_max)
                N_avg, image_avg3 = render.render_hist3d(self.locs, self.oversampling, self.t_min, self.t_min, self.t_max, self.t_max, self.z_min, self.z_max, self.pixelsize)
                n_pixel, _ = image_avg.shape
                image_half = n_pixel / 2
                CF_image_avg = np.conj(np.fft.fft2(image_avg)) #TODO: Check what this does actually for the 3d image
                CF_image_avg3 = image_avg3
                # TODO: blur auf average !!!

                fc = functools.partial(align_group3d, angles, self.oversampling, self.t_min, self.t_max, self.z_min, self.z_max, CF_image_avg3, CF_image_avg, image_half, self.pixelsize, counter, lock)
                result = pool3d.map_async(fc, range(n_groups), groups_per_worker)
                while not result.ready():
                    self.progressMade.emit(it+1, self.iterations, counter.value, n_groups, self.locs, False)
                    time.sleep(0.5)
                self.locs.x = np.ctypeslib.as_array(x)
                self.locs.y = np.ctypeslib.as_array(y)
                self.locs.z = np.ctypeslib.as_array(z)
                self.locs.x -= np.mean(self.locs.x)
                self.locs.y -= np.mean(self.locs.y)
                self.locs.z -= np.mean(self.locs.z)
                #Second part of alignment
                print('2nd alignment')
                counter.value = 0
                N_avg, image_avg = render.render_histz(self.locs, self.oversampling, self.t_min, self.z_min, self.t_max, self.z_max,self.pixelsize)
                print('---Size---')
                print(image_avg.shape)
                N_avg, image_avg3 = render.render_hist3d(self.locs, self.oversampling, self.t_min, self.t_min, self.t_max, self.t_max, self.z_min, self.z_max, self.pixelsize)
                n_pixelx, n_pixelz = image_avg.shape
                image_half = n_pixelz / 2

                CF_image_avg = np.conj(np.fft.fft2(image_avg)) #TODO: Check what this does actually for the 3d image
                print(CF_image_avg.shape)
                CF_image_avg3 = image_avg3
                # TODO: blur auf average !!!
                #fig = plt.figure()
                #fig.canvas.set_window_title('image avg')
                #ax = fig.add_subplot(111)
                #ax.imshow(image_avg)
                #plt.show()
                fc = functools.partial(align_group3d_2, angles, self.oversampling, self.t_min, self.t_max, self.z_min, self.z_max, CF_image_avg3, CF_image_avg, image_half, self.pixelsize, counter, lock)
                result = pool3d.map_async(fc, range(n_groups), groups_per_worker)
                while not result.ready():
                    self.progressMade.emit(it+1, self.iterations, counter.value, n_groups, self.locs, False)
                    time.sleep(0.5)
                self.locs.x = np.ctypeslib.as_array(x)
                self.locs.y = np.ctypeslib.as_array(y)
                self.locs.z = np.ctypeslib.as_array(z)
                self.locs.x -= np.mean(self.locs.x)
                self.locs.y -= np.mean(self.locs.y)
                self.locs.z -= np.mean(self.locs.z)
                self.progressMade.emit(it+1, self.iterations, counter.value, n_groups, self.locs, True)

        else:
            n_groups = self.group_index.shape[0]
            a_step = np.arcsin(1 / (self.oversampling * self.r))
            angles = np.arange(0, 2*np.pi, a_step)
            n_workers = max(1, int(0.75 * multiprocessing.cpu_count()))
            manager = multiprocessing.Manager()
            counter = manager.Value('d', 0)
            lock = manager.Lock()
            groups_per_worker = max(1, int(n_groups / n_workers))
            for it in range(self.iterations):
                counter.value = 0
                # render average image
                N_avg, image_avg = render.render_hist(self.locs, self.oversampling, self.t_min, self.t_min, self.t_max, self.t_max)
                n_pixel, _ = image_avg.shape
                image_half = n_pixel / 2
                CF_image_avg = np.conj(np.fft.fft2(image_avg))
                # TODO: blur auf average !!!
                fc = functools.partial(align_group, angles, self.oversampling, self.t_min, self.t_max, CF_image_avg, image_half, counter, lock)
                result = pool.map_async(fc, range(n_groups), groups_per_worker)
                while not result.ready():
                    self.progressMade.emit(it+1, self.iterations, counter.value, n_groups, self.locs, False)
                    time.sleep(0.5)
                self.locs.x = np.ctypeslib.as_array(x)
                self.locs.y = np.ctypeslib.as_array(y)
                self.locs.x -= np.mean(self.locs.x)
                self.locs.y -= np.mean(self.locs.y)
                self.progressMade.emit(it+1, self.iterations, counter.value, n_groups, self.locs, True)

class ParametersDialog(QtGui.QDialog):

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.setWindowTitle('Parameters')
        self.setModal(False)
        grid = QtGui.QGridLayout(self)

        grid.addWidget(QtGui.QLabel('Oversampling:'), 0, 0)
        self.oversampling = QtGui.QDoubleSpinBox()
        self.oversampling.setRange(1, 1e7)
        self.oversampling.setValue(10)
        self.oversampling.setDecimals(1)
        self.oversampling.setKeyboardTracking(False)
        self.oversampling.valueChanged.connect(self.window.view.update_image)
        grid.addWidget(self.oversampling, 0, 1)

        grid.addWidget(QtGui.QLabel('Iterations:'), 1, 0)
        self.iterations = QtGui.QSpinBox()
        self.iterations.setRange(0, 1e7)
        self.iterations.setValue(10)
        grid.addWidget(self.iterations, 1, 1)


class View(QtGui.QLabel):

    def __init__(self, window):
        super().__init__()
        self.window = window
        self.setMinimumSize(1, 1)
        self.setAlignment(QtCore.Qt.AlignCenter)
        self.setAcceptDrops(True)
        self._pixmap = None
        self.running = False

    def average(self):
        if not self.running:
            self.running = True
            oversampling = self.window.parameters_dialog.oversampling.value()
            iterations = self.window.parameters_dialog.iterations.value()
            self.thread = Worker(self.locs, self.r, self.group_index, oversampling, iterations, self.pixelsize, self.r_z)
            self.thread.progressMade.connect(self.on_progress)
            self.thread.finished.connect(self.on_finished)
            self.thread.start()

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.accept()
        else:
            event.ignore()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        path = urls[0].toLocalFile()
        ext = os.path.splitext(path)[1].lower()
        if ext == '.hdf5':
            self.open(path)

    def on_finished(self):
        self.window.status_bar.showMessage('Done!')
        self.running = False

    def on_progress(self, it, total_it, g, n_groups, locs, update_image):
        self.locs = locs.copy()
        if update_image:
            self.update_image()
        self.window.status_bar.showMessage('Iteration {:,}/{:,}, Group {:,}/{:,}'.format(it, total_it, g, n_groups))

    def open(self, path):
        self.path = path
        try:
            self.locs, self.info = io.load_locs(path, qt_parent=self)
        except io.NoMetadataFileError:
            return
        groups = np.unique(self.locs.group)
        n_groups = len(groups)
        n_locs = len(self.locs)
        has_z = hasattr(self.locs[0], 'z')
        self.has_z = has_z
        if has_z:
            pixelsize, ok = QtGui.QInputDialog.getInt(self, 'Pixelsize Dialog',
            'Enter Pixelsize:', 160)
        else:
            self.pixelsize = 0 #default value for 2d cases
        if ok:
            print(pixelsize)
            self.pixelsize = pixelsize
        self.group_index = scipy.sparse.lil_matrix((n_groups, n_locs), dtype=np.bool)
        progress = lib.ProgressDialog('Creating group index', 0, len(groups), self)
        progress.set_value(0)
        for i, group in enumerate(groups):
            index = np.where(self.locs.group == group)[0]
            self.group_index[i, index] = True
            progress.set_value(i+1)
        progress = lib.ProgressDialog('Aligning by center of mass', 0, len(groups), self)
        progress.set_value(0)
        for i in range(n_groups):
            index = self.group_index[i, :].nonzero()[1]
            self.locs.x[index] -= np.mean(self.locs.x[index])
            self.locs.y[index] -= np.mean(self.locs.y[index])
            if has_z:
                self.locs.z[index] -= np.mean(self.locs.z[index])
            progress.set_value(i+1)


        self.r = 2 * np.sqrt(np.mean(self.locs.x**2 + self.locs.y**2))
        self.r_z = 0
        if has_z:
            self.r_z = 2 * 3  * np.std(self.locs.z) # 2 std deviations radius for now
            self.plot3d()
            print('3D mode activated')

        self.update_image()

        if has_z:
            status = lib.StatusDialog('Starting parallel pool...', self.window)
            global pool3d, x, y, z
            try:
                pool3d.close()
            except NameError:
                pass
            x = sharedctypes.RawArray('f', self.locs.x)
            y = sharedctypes.RawArray('f', self.locs.y)
            z = sharedctypes.RawArray('f', self.locs.z)
            n_workers = max(1, int(0.75 * multiprocessing.cpu_count()))
            #n_workers = 1
            pool3d = multiprocessing.Pool(n_workers, init_pool3, (x, y, z, self.group_index))
            self.window.status_bar.showMessage('Ready for processing!')
            status.close()
        else:
            status = lib.StatusDialog('Starting parallel pool...', self.window)
            global pool, x, y
            try:
                pool.close()
            except NameError:
                pass
            x = sharedctypes.RawArray('f', self.locs.x)
            y = sharedctypes.RawArray('f', self.locs.y)
            n_workers = max(1, int(0.75 * multiprocessing.cpu_count()))
            pool = multiprocessing.Pool(n_workers, init_pool, (x, y, self.group_index))
            self.window.status_bar.showMessage('Ready for processing!')
            status.close()

    def resizeEvent(self, event):
        if self._pixmap is not None:
            self.set_pixmap(self._pixmap)

    def save(self, path):
        merge_groups = QtGui.QMessageBox.question(self, 'Save', 'Merge groups into single particle?',
                                                  QtGui.QMessageBox.Yes, QtGui.QMessageBox.No)
        cx = self.info[0]['Width'] / 2
        cy = self.info[0]['Height'] / 2
        self.locs.x += cx
        self.locs.y += cy
        info = self.info + [{'Generated by': 'Picasso Average'}]
        if merge_groups == QtGui.QMessageBox.Yes:
            out_locs = lib.remove_from_rec(self.locs, 'group')
        else:
            out_locs = self.locs
        io.save_locs(path, out_locs, info)

    def set_image(self, image):
        cmap = np.uint8(np.round(255 * plt.get_cmap('magma')(np.arange(256))))
        image /= image.max()
        image = np.minimum(image, 1.0)
        image = np.round(255 * image).astype('uint8')
        Y, X = image.shape
        self._bgra = np.zeros((Y, X, 4), dtype=np.uint8, order='C')
        self._bgra[..., 0] = cmap[:, 2][image]
        self._bgra[..., 1] = cmap[:, 1][image]
        self._bgra[..., 2] = cmap[:, 0][image]
        qimage = QtGui.QImage(self._bgra.data, X, Y, QtGui.QImage.Format_RGB32)
        self._pixmap = QtGui.QPixmap.fromImage(qimage)
        self.set_pixmap(self._pixmap)

    def set_pixmap(self, pixmap):
        self.setPixmap(pixmap.scaled(self.width(), self.height(), QtCore.Qt.KeepAspectRatio, QtCore.Qt.FastTransformation))

    def update_image(self, *args):
        oversampling = self.window.parameters_dialog.oversampling.value()
        t_min = -self.r
        t_max = self.r
        N_avg, image_avg = render.render_hist(self.locs, oversampling, t_min, t_min, t_max, t_max)
        self.set_image(image_avg)
        has_z = hasattr(self.locs[0], 'z')
        if has_z:
            print('Updating.. 3d plot')
            self.update3dplot()

    def plot3d(self):
        locs = self.locs
        locs = stack_arrays(locs, asrecarray=True, usemask=False)
        fig = plt.figure()
        self.fig = fig
        print(fig)
        fig.canvas.set_window_title('3D - Average')
        ax = fig.add_subplot(111, projection='3d')
        self.ax = ax
        ax.set_title('3D Average')
        maxplot = 1000;

        xlocs = locs['x']
        ylocs = locs['y']
        zlocs = locs['z']
        colors = locs['z'][:]
        sampling = np.round(len(xlocs)/maxplot)
        xlocs = xlocs[0::sampling]
        ylocs = ylocs[0::sampling]
        zlocs = zlocs[0::sampling]
        colors = colors[0::sampling]

        colors[colors > np.mean(zlocs)+3*np.std(zlocs)]=np.mean(zlocs)+3*np.std(zlocs)
        colors[colors < np.mean(zlocs)-3*np.std(zlocs)]=np.mean(zlocs)-3*np.std(zlocs)
        ax.scatter(xlocs, ylocs, zlocs,c=colors,cmap='jet')
        ax.set_xlabel('X [Px]')
        ax.set_ylabel('Y [Px]')
        ax.set_zlabel('Z [nm]')
        ax.set_xlim( np.mean(xlocs)-3*np.std(xlocs), np.mean(xlocs)+3*np.std(xlocs))
        ax.set_ylim( np.mean(ylocs)-3*np.std(ylocs), np.mean(ylocs)+3*np.std(ylocs))
        ax.set_zlim( np.mean(zlocs)-3*np.std(zlocs), np.mean(zlocs)+3*np.std(zlocs))

        plt.axis('equal')
        plt.show()

    def update3dplot(self):
        locs = self.locs
        locs = stack_arrays(locs, asrecarray=True, usemask=False)
        fig = self.fig
        ax = self.ax
        ax.cla()
        ax.set_title('3D Average')
        maxplot = 1000;

        xlocs = locs['x']
        ylocs = locs['y']
        zlocs = locs['z']
        colors = locs['z'][:]
        sampling = np.round(len(xlocs)/maxplot)
        xlocs = xlocs[0::sampling]
        ylocs = ylocs[0::sampling]
        zlocs = zlocs[0::sampling]
        colors = colors[0::sampling]

        colors[colors > np.mean(zlocs)+3*np.std(zlocs)]=np.mean(zlocs)+3*np.std(zlocs)
        colors[colors < np.mean(zlocs)-3*np.std(zlocs)]=np.mean(zlocs)-3*np.std(zlocs)



        ax.scatter(xlocs, ylocs, zlocs,c=colors,cmap='jet')
        ax.set_xlabel('X [Px]')
        ax.set_ylabel('Y [Px]')
        ax.set_zlabel('Z [nm]')
        ax.set_xlim( np.mean(xlocs)-3*np.std(xlocs), np.mean(xlocs)+3*np.std(xlocs))
        ax.set_ylim( np.mean(ylocs)-3*np.std(ylocs), np.mean(ylocs)+3*np.std(ylocs))
        ax.set_zlim( np.mean(zlocs)-3*np.std(zlocs), np.mean(zlocs)+3*np.std(zlocs))
        fig.canvas.draw()
        plt.axis('equal')
        #plt.show()



class Window(QtGui.QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle('Picasso: Average')
        self.resize(512, 512)
        this_directory = os.path.dirname(os.path.realpath(__file__))
        icon_path = os.path.join(this_directory, 'icons', 'average.ico')
        icon = QtGui.QIcon(icon_path)
        self.setWindowIcon(icon)
        self.view = View(self)
        self.setCentralWidget(self.view)
        self.parameters_dialog = ParametersDialog(self)
        menu_bar = self.menuBar()
        file_menu = menu_bar.addMenu('File')
        open_action = file_menu.addAction('Open')
        open_action.setShortcut(QtGui.QKeySequence.Open)
        open_action.triggered.connect(self.open)
        file_menu.addAction(open_action)
        save_action = file_menu.addAction('Save')
        save_action.setShortcut(QtGui.QKeySequence.Save)
        save_action.triggered.connect(self.save)
        file_menu.addAction(save_action)
        process_menu = menu_bar.addMenu('Process')
        parameters_action = process_menu.addAction('Parameters')
        parameters_action.setShortcut('Ctrl+P')
        parameters_action.triggered.connect(self.parameters_dialog.show)
        average_action = process_menu.addAction('Average')
        average_action.setShortcut('Ctrl+A')
        average_action.triggered.connect(self.view.average)
        self.status_bar = self.statusBar()

    def open(self):
        path = QtGui.QFileDialog.getOpenFileName(self, 'Open localizations', filter='*.hdf5')
        if path:
            self.view.open(path)

    def save(self):
        out_path = os.path.splitext(self.view.path)[0] + '_avg.hdf5'
        path = QtGui.QFileDialog.getSaveFileName(self, 'Save localizations', out_path, filter='*.hdf5')
        if path:
            self.view.save(path)


def main():

    app = QtGui.QApplication(sys.argv)
    window = Window()
    window.show()

    def excepthook(type, value, tback):
        lib.cancel_dialogs()
        message = ''.join(traceback.format_exception(type, value, tback))
        errorbox = QtGui.QMessageBox.critical(window, 'An error occured', message)
        errorbox.exec_()
        sys.__excepthook__(type, value, tback)
    sys.excepthook = excepthook

    sys.exit(app.exec_())


if __name__ == '__main__':
    main()
