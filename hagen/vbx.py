# -*- coding: utf-8 -*-
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Происхождение:
#   L. Burget, P. Pálka (BUT Speech@FIT) — VB_diarization.py из
#   https://github.com/BUTSpeechFIT/VBx;
#   H. Bredin (CNRS) — сокращённая версия в pyannote.audio 4.0
#   (pyannote/audio/utils/vbx.py, без HMM);
#   перенесено в Hagen без изменения вычислений, 2026.
"""Группировка голосов VBx поверх PLDA — так, как её делает community-1.

Отпечатки голоса сначала переводятся в пространство PLDA (центрирование,
отбеливание, LDA), затем байесовская смесь уточняет начальное разбиение,
полученное агломеративной группировкой. Лишние голоса у смеси вымирают сами:
их доля `pi` уходит в ноль. Это и решает, сколько людей в записи.

Только numpy и scipy — ни torch, ни pyannote.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

import numpy as np
from scipy.linalg import eigh
from scipy.special import logsumexp, softmax


def vbx(X: np.ndarray, Phi: np.ndarray, Fa: float = 1.0, Fb: float = 1.0,
        pi: int | np.ndarray = 10, gamma: np.ndarray | None = None,
        max_iters: int = 10, epsilon: float = 1e-4) -> tuple[np.ndarray, np.ndarray, list]:
    """Вариационный байесовский вывод для смеси голосов.

    X      — (T, D) отпечатки в пространстве PLDA;
    Phi    — (D,) диагональ межклассовой ковариации;
    Fa, Fb — масштаб статистик и штраф за лишних говорящих: чем больше Fb,
             тем меньше голосов остаётся;
    pi     — число мест для голосов или начальные доли;
    gamma  — начальные доли принадлежности (T, S).

    Возвращает (gamma, pi, ELBO по итерациям).

    Landini F., Profant J., Diez M., Burget L.: Bayesian HMM clustering of
    x-vector sequences (VBx) in speaker diarization: theory, implementation and
    analysis on standard tasks. Номера формул в комментариях — из этой статьи.
    """
    D = X.shape[1]

    if isinstance(pi, int):
        pi = np.ones(pi) / pi

    if gamma is None:
        # в Hagen всегда задаётся начальное разбиение; случайного старта нет
        raise ValueError("vbx: нужно начальное разбиение gamma")

    assert gamma.shape[1] == len(pi) and gamma.shape[0] == X.shape[0]

    G = -0.5 * (np.sum(X ** 2, axis=1, keepdims=True) + D * np.log(2 * np.pi))  # (23)
    V = np.sqrt(Phi)                                                             # (5)-(6)
    rho = X * V                                                                  # (18)
    Li: list = []
    for ii in range(max_iters):
        invL = 1.0 / (1 + Fa / Fb * gamma.sum(axis=0, keepdims=True).T * Phi)   # (17)
        alpha = Fa / Fb * invL * gamma.T.dot(rho)                                # (16)
        log_p_ = Fa * (rho.dot(alpha.T) - 0.5 * (invL + alpha ** 2).dot(Phi) + G)  # (23)

        eps = 1e-8
        lpi = np.log(pi + eps)
        log_p_x = logsumexp(log_p_ + lpi, axis=-1)
        log_pX_ = np.sum(log_p_x, axis=0)

        gamma = np.exp(log_p_ + lpi - log_p_x[:, None])
        pi = np.sum(gamma, axis=0)
        pi = pi / pi.sum()

        elbo = log_pX_ + Fb * 0.5 * np.sum(np.log(invL) - invL - alpha ** 2 + 1)  # (25)
        Li.append([elbo])

        if ii > 0 and elbo - Li[-2][0] < epsilon:
            break
    return gamma, pi, Li


def cluster_vbx(ahc_init: np.ndarray, fea: np.ndarray, Phi: np.ndarray, Fa: float,
                Fb: float, max_iters: int = 20,
                init_smoothing: float = 7.0) -> tuple[np.ndarray, np.ndarray]:
    """VBx от начального разбиения AHC. Возвращает (gamma (T, S), pi (S,))."""
    qinit = np.zeros((len(ahc_init), ahc_init.max() + 1))
    qinit[range(len(ahc_init)), ahc_init.astype(int)] = 1.0
    qinit = qinit if init_smoothing < 0 else softmax(qinit * init_smoothing, axis=1)
    gamma, pi, _ = vbx(fea, Phi, Fa=Fa, Fb=Fb, pi=qinit.shape[1], gamma=qinit,
                       max_iters=max_iters)
    return gamma, pi


def l2_norm(x: np.ndarray) -> np.ndarray:
    """Нормировка вектора или строк матрицы на единичную длину."""
    if x.ndim == 1:
        return x / np.linalg.norm(x)
    if x.ndim == 2:
        return x / np.linalg.norm(x, axis=1, ord=2)[:, np.newaxis]
    raise ValueError("l2_norm: ждали 1 или 2 измерения, пришло %d" % x.ndim)


class PLDA:
    """Перевод отпечатков в пространство PLDA по двум файлам community-1."""

    def __init__(self, transform_npz: Path | str, plda_npz: Path | str,
                 lda_dimension: int = 128):
        x = np.load(transform_npz)
        mean1, mean2, lda = x["mean1"], x["mean2"], x["lda"]

        p = np.load(plda_npz)
        plda_mu, plda_tr, plda_psi = p["mu"], p["tr"], p["psi"]

        # внутриклассовая и межклассовая ковариации (W, B)
        W = np.linalg.inv(plda_tr.T.dot(plda_tr))
        B = np.linalg.inv((plda_tr.T / plda_psi).dot(plda_tr))

        # обобщённая задача на собственные значения: отбеливание и сортировка
        acvar, wccn = eigh(B, W)
        plda_psi = acvar[::-1]
        plda_tr = wccn.T[::-1]

        self._xvec_tf: Callable[[np.ndarray], np.ndarray] = lambda v: np.sqrt(lda.shape[1]) * l2_norm(
            lda.T.dot(np.sqrt(lda.shape[0]) * l2_norm(v - mean1).T).T - mean2
        )
        self._plda_mu = plda_mu
        self._plda_tr = plda_tr
        self._plda_psi = plda_psi
        self.lda_dimension = int(lda_dimension)

    @property
    def phi(self) -> np.ndarray:
        """Межклассовая ковариация в пространстве PLDA."""
        return self._plda_psi[: self.lda_dimension]

    def __call__(self, embeddings: np.ndarray) -> np.ndarray:
        """(N, 256) отпечатков → (N, lda_dimension) в пространстве PLDA."""
        x0 = self._xvec_tf(embeddings)
        return (x0 - self._plda_mu).dot(self._plda_tr.T)[:, : self.lda_dimension]
