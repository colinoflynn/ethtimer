# Copyright 2026 Colin O'Flynn
# SPDX-License-Identifier: Apache-2.0
"""Drive a `Target` against a victim and check the result against the real key.

    from ethtimer import Device, campaign
    with Device("COM10") as d:
        d.set_net(ip="192.168.7.20", mask="255.255.255.0",
                  gw="192.168.7.1", victim="192.168.7.10")
        res = campaign.run(d, MyTarget(), n=20000)
    print(res.summary())
    res.save("run.npz")

WHY THIS EXISTS. Without it, pointing the instrument at a protocol means
copying a capture script and editing it, and the copies diverge: the firmwares
this module grew out of were one instrument per protocol and came to differ by
hundreds of lines, so a fix in one never reached the others. The parts that are
the same for every protocol are here exactly once: the oneshot-before-a-batch
that warms ARP and sizes the window, the batching, the diagnostics, the
acceptance check and the saved file layout. A protocol contributes a `Target`
and nothing else.

THE ACCEPTANCE CHECK IS THE POINT. `run()` finishes by encrypting each captured
AES input under the victim's real key and comparing against the captured output.
A record count says the plumbing moved bytes; this says the bytes are the ones
the victim's AES actually produced, which is the only statement that catches a
window landing four bytes off, a reply parsed against a stale offset, or a
request the victim quietly rejected. It is refused rather than skipped when the
target has no truth key -- see `Result.verified`.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

import numpy as np

from . import proto as P
from .aes import aes128_ecb as _aes_ecb
from .target import MODE_BANK, MODE_FIXED, MODE_RELAY, Blocks, Target


@dataclass
class Result:
    """A capture and what could be said about it."""
    target: str
    blocks: Blocks
    window: np.ndarray
    dt_hw: np.ndarray
    dt_sw: np.ndarray
    seq: np.ndarray
    #: For each record, which exchange of the capture produced it. Not
    #: `arange(n)`: a dropped exchange still consumed a request.
    index: np.ndarray
    clock_hz: int
    win_off: int
    win_len: int
    gap_us: int
    n_requested: int
    n_timeout: int
    n_short: int
    n_bad_dt: int
    n_dropped: int
    elapsed_s: float
    board: str
    #: True only if a real key was available AND every spot-checked block matched.
    verified: bool = False
    #: Human-readable verdict, always set.
    verdict: str = "not checked"
    checked: int = 0
    meta: dict = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.dt_hw)

    def us(self) -> np.ndarray:
        return self.dt_hw.astype(np.float64) / self.clock_hz * 1e6

    @property
    def rate(self) -> float:
        return len(self) / self.elapsed_s if self.elapsed_s else 0.0

    def summary(self) -> str:
        us = self.us()
        return ("%s: %d records of %d requested, %d timeouts, %d short, "
                "%d bad dt, %d dropped, %.0f/s, median %.3f us, sd %.3f us"
                % (self.target, len(self), self.n_requested, self.n_timeout,
                   self.n_short, self.n_bad_dt, self.n_dropped, self.rate,
                   float(np.median(us)) if len(us) else float("nan"),
                   float(us.std()) if len(us) else float("nan")))

    def save(self, path: str) -> str:
        """Write the capture in the layout every adapter shares.

        `aes_in` / `aes_out` are `(n, k, 16)`. Downstream analysis that wants the
        old one-block-per-exchange shape takes `[:, 0, :]`; nothing has to guess
        which protocol produced the file, because `target` and `blocks_per_exchange`
        are in it.
        """
        arrays = dict(
            aes_in=self.blocks.aes_in, aes_out=self.blocks.aes_out,
            window=self.window, dt_hw=self.dt_hw, dt_sw=self.dt_sw,
            seq=self.seq, index=self.index,
            clock_hz=np.int64(self.clock_hz),
            # Two aliases for the same number. `clock_hz` alone invites the
            # reader to wonder whose clock; it is the INSTRUMENT's, since every
            # `dt_hw` is a count of its cycles, and `BATCH_HDR` carries it so
            # that two boards at different frequencies produce comparable
            # microseconds.
            #
            # `attacker_hz` is kept for analysis written against an earlier
            # layout, and kept rather than dropped for a specific reason: at
            # least one such reader falls back to a HARD-CODED 216 MHz when the
            # key is absent. Dropping the alias would not raise there -- it
            # would scale an F429 capture's 180 MHz cycles by 216 MHz and
            # report every duration 20 % short, with no error at all. An alias
            # costs eight bytes in the file.
            instrument_hz=np.int64(self.clock_hz),
            attacker_hz=np.int64(self.clock_hz),
            win_off=np.int64(self.win_off),
            win_len=np.int64(self.win_len), gap_us=np.int64(self.gap_us),
            n_requested=np.int64(self.n_requested),
            n_timeout=np.int64(self.n_timeout), n_short=np.int64(self.n_short),
            n_bad_dt=np.int64(self.n_bad_dt),
            n_dropped=np.int64(self.n_dropped),
            blocks_per_exchange=np.int64(self.blocks.per_exchange),
            target=np.array(self.target), board=np.array(self.board),
            verified=np.array(self.verified), verdict=np.array(self.verdict),
        )
        arrays.update(self.blocks.extra)
        arrays.update({k: v for k, v in self.meta.items()
                       if isinstance(v, np.ndarray)})
        np.savez(path, **arrays)
        return path


class _Partial:
    """Append each batch to disk as it lands, so a long capture can be salvaged.

    WHY. A TLS capture is ONE session for its whole length, because every
    exchange has to be protected by the same record key -- so a session that
    dies at minute eighteen of twenty-one costs the entire run. Incremental
    saving is the difference between losing the run and losing the tail of it.

    Records are written in the DEVICE's own layout -- `window || dt_hw || dt_sw
    || seq`, `rec_len` bytes each -- so the file is a flat array of fixed-size
    records with no framing to get wrong, and a truncated tail is a whole number
    of records short rather than a corrupt file. The metadata that cannot be
    derived from it (window geometry, clock, and whatever the target contributes,
    such as the known plaintext and the ground-truth key) goes in a sidecar
    written once.

    Both files are removed on a clean finish: their only job is to exist when
    the run does not reach its end.
    """

    def __init__(self, out, win_off, win_len, clock_hz, gap_us, target, extra):
        self.path = out + ".part"
        self.meta = out + ".partmeta.npz"
        self.rec_len = win_len + P.REC_OVERHEAD
        self.n = 0
        self._fh = open(self.path, "wb")
        md = dict(rec_len=np.int64(self.rec_len), win_off=np.int64(win_off),
                  win_len=np.int64(win_len), clock_hz=np.int64(clock_hz),
                  gap_us=np.int64(gap_us), target=np.array(target))
        # np.int64(x) is an np.generic, NOT an np.ndarray, so a filter on
        # ndarray alone silently drops every scalar a target contributes --
        # which is how the first version of this lost the window geometry it
        # needed to decode.
        md.update({k: v for k, v in extra.items()
                   if isinstance(v, (np.ndarray, np.generic))})
        np.savez(self.meta, **md)

    def add(self, batch, index):
        """One batch, re-serialised into the device's record layout."""
        if not len(batch):
            return
        tail = np.stack([batch.dt_hw, batch.dt_sw,
                         index.astype(np.uint32)], axis=1)
        rec = np.concatenate([batch.window,
                              tail.view(np.uint8).reshape(len(batch), 12)],
                             axis=1)
        self._fh.write(rec.tobytes())
        self._fh.flush()
        self.n += len(batch)

    def done(self):
        try:
            self._fh.close()
        except Exception:
            pass
        for f in (self.path, self.meta):
            try:
                os.remove(f)
            except OSError:
                pass

    def abandon(self):
        try:
            self._fh.close()
        except Exception:
            pass
        print("  partial capture kept: %s (%d records) + %s"
              % (self.path, self.n, self.meta))


def load_partial(out: str):
    """Read back what a dead capture left behind. -> (arrays dict, meta dict).

    `seq` in the partial file is the GLOBAL exchange index, not the per-batch
    one, because that is what a reader needs and what the batch boundaries
    would otherwise have to be reconstructed to recover.
    """
    meta = dict(np.load(out + ".partmeta.npz", allow_pickle=False))
    rec_len = int(meta["rec_len"])
    win_len = int(meta["win_len"])
    raw = np.fromfile(out + ".part", dtype=np.uint8)
    n = len(raw) // rec_len
    if n * rec_len != len(raw):
        print("  %d trailing bytes ignored: the file ends mid-record"
              % (len(raw) - n * rec_len))
    a = raw[:n * rec_len].reshape(n, rec_len)
    tail = a[:, win_len:win_len + 12].copy().view(np.uint32)
    return dict(window=a[:, :win_len].copy(), dt_hw=tail[:, 0].copy(),
                dt_sw=tail[:, 1].copy(), index=tail[:, 2].astype(np.int64)), meta


def _progress(label):
    def f(got, total):
        print("  %s %d/%d" % (label, got, total), flush=True)
    return f


def run(dev, target: Target, n: int, gap_us: int = 200,
        progress: bool = True, check_link: bool = True,
        spot_check: int = 256, out: str = "") -> Result:
    """Prepare, capture `n` exchanges, decode and check. Raises on a bad capture.

    `spot_check` bounds how many exchanges get encrypted under the real key; the
    check is O(n) in numpy but the point is made long before n is large, and a
    full pass on a million-record capture is a minute of nothing.
    """
    if target.mode not in (MODE_FIXED, MODE_BANK, MODE_RELAY):
        raise ValueError("target %r has mode %r" % (target.name, target.mode))

    info = dev.info()
    print(info)
    dev.set_target(port=target.port, timeout_ms=target.timeout_ms,
                   protocol=target.transport)

    if target.transport == P.PROTO_TCP:
        dev.tcp_connect()

    target.prepare(dev)

    t0 = time.time()
    if target.mode == MODE_RELAY:
        window, dt_hw, dt_sw, seq, diag = _run_relay(dev, target, n, progress)
    else:
        window, dt_hw, dt_sw, seq, diag = _run_batched(
            dev, target, n, gap_us, progress, check_link, out=out)
    win_off, win_len = diag["win_off"], diag["win_len"]
    index = diag["index"]
    elapsed = time.time() - t0

    if target.transport == P.PROTO_TCP:
        dev.tcp_close()

    if len(dt_hw) == 0:
        raise RuntimeError(
            "no records at all: %d timeouts, %d short. A whole batch of "
            "timeouts means the victim is not answering; a whole batch of "
            "short means the window does not fit its replies."
            % (diag["n_timeout"], diag["n_short"]))

    blocks = target.blocks(window, index)
    if blocks.n != len(dt_hw):
        raise RuntimeError("target %r returned %d block rows for %d records"
                           % (target.name, blocks.n, len(dt_hw)))

    keep = np.asarray(blocks.valid, bool)
    n_dropped = int((~keep).sum())
    if n_dropped:
        # EVERY per-record array is filtered, including the adapter's own extras.
        # Leaving those unfiltered puts arrays of different lengths in one file,
        # and the analysis that reads `ciphertext` and `keystream2` alongside
        # `dt_hw` then pairs a timing with a different exchange's bytes -- which
        # is not a crash, it is a capture that quietly recovers nothing.
        extra = {k: (v[keep] if isinstance(v, np.ndarray)
                     and v.ndim >= 1 and len(v) == len(keep) else v)
                 for k, v in blocks.extra.items()}
        blocks = Blocks(aes_in=blocks.aes_in[keep], aes_out=blocks.aes_out[keep],
                        valid=np.ones(int(keep.sum()), bool), extra=extra)
        window, dt_hw = window[keep], dt_hw[keep]
        dt_sw, seq, index = dt_sw[keep], seq[keep], index[keep]

    # A HIGH DROP RATE IS A FAILED CAPTURE, not a footnote in the summary. The
    # records that survive still verify -- that is what makes it dangerous: the
    # acceptance check runs on what is left and passes. A 250 000-exchange
    # SNMPv3 capture returned 75 534 records this way, because the victim
    # stopped answering with the encrypted response 138 seconds in and every
    # later reply was an unencrypted report.
    if n_dropped and n_dropped > 0.02 * diag["n_requested"]:
        raise RuntimeError(
            "%d of %d exchanges (%.1f%%) produced no usable block. The %d that "
            "did will still verify, which is exactly why this is raised rather "
            "than reported: the acceptance check only ever sees the survivors. "
            "Look at what the victim was actually replying with -- a protocol "
            "that stops answering the way it started is the usual cause."
            % (n_dropped, diag["n_requested"],
               100.0 * n_dropped / max(1, diag["n_requested"]), len(dt_hw)))

    res = Result(
        target=target.name, blocks=blocks, window=window, dt_hw=dt_hw,
        dt_sw=dt_sw, seq=seq, index=index,
        clock_hz=diag["clock_hz"], win_off=win_off,
        win_len=win_len, gap_us=gap_us, n_requested=diag["n_requested"],
        n_timeout=diag["n_timeout"], n_short=diag["n_short"],
        n_bad_dt=diag.get("n_bad_dt", 0),
        n_dropped=n_dropped, elapsed_s=elapsed, board=info.board,
        meta=dict(diag.get("meta", {})),
    )
    res.blocks.extra.update(target.extra_arrays())
    _check(res, target, spot_check)
    return res


def _run_batched(dev, target, n, gap_us, progress, check_link, out=""):
    """MODE_FIXED and MODE_BANK: the device runs, the host waits."""
    it = target.requests()

    # ONE EXCHANGE BEFORE THE BATCH. It warms the ARP entry, proves the victim
    # answers this request, and -- the part that is not optional -- gives a real
    # reply to size the window against. A target that already has one from its
    # own `prepare()` supplies it and the probe is skipped, which is how TCP
    # targets avoid a UDP-only `oneshot` they cannot use.
    reply = target.sample_reply()
    first = None
    if reply is None:
        if target.transport == P.PROTO_TCP:
            raise RuntimeError(
                "a TCP target must return a reply from sample_reply(): the "
                "probe below is a UDP oneshot and there is no stream "
                "equivalent of it.")
        first = next(it)
        reply = b""
        for _ in range(10):
            reply = dev.oneshot(first)
            if reply:
                break
            time.sleep(0.3)
        if not reply:
            raise RuntimeError(
                "the victim did not answer %d probe requests on port %d. Check "
                "it is running, that its IP matches the device's victim "
                "address, and that the request this target builds is one it "
                "accepts." % (10, target.port))

    # A target may also say how many reply bytes to WAIT for, which is what TCP
    # needs: a datagram arrives whole, a stream does not.
    w = target.window(reply)
    off, length, want = (w if len(w) == 3 else (w[0], w[1], 0))
    if off + length > len(reply):
        raise RuntimeError(
            "target %r wants bytes [%d:%d] of a %d-byte reply -- the window "
            "does not fit. Every exchange would be counted short."
            % (target.name, off, off + length, len(reply)))
    if length > P.MAX_WIN:
        raise RuntimeError("window of %d bytes exceeds the device's %d"
                           % (length, P.MAX_WIN))
    print("reply %d B, keeping [%d:%d] (%d B)%s"
          % (len(reply), off, off + length, length,
             ", waiting for %d B" % want if want else ""))
    dev.set_window(off=off, length=length, want=want)

    # Progress from 2 000 rather than 20 000: a banked TCP capture spends
    # seconds per bank and silence for a minute reads as a hang.
    cb = _progress(target.name) if progress and n > 2000 else None

    # Written as batches land, removed on a clean finish. The only capture that
    # really needs it is TLS -- one session for the whole run, so a session that
    # dies late costs everything -- but there is no reason to make it special.
    part = _Partial(out, off, length, dev.info().clock_hz, gap_us,
                    target.name, target.extra_arrays()) if out else None

    if target.mode == MODE_FIXED:
        if first is None:
            first = next(it)
        target.note_sent(0, [first])
        dev.set_request(first)
        # Chunked HERE rather than by `Device.capture()`, which joins its
        # sub-batches into one Batch and so loses their boundaries -- and with
        # them the only thing that turns a per-batch `seq` into a global
        # exchange index. `capture()` remains the right call for a caller who
        # wants records and diagnostics and no index; this one wants the index.
        parts, got, sent = [], 0, 0
        cap = dev.batch_capacity()
        try:
            while got < n:
                b = dev.run(min(cap, n - got), gap_us=gap_us,
                            check_link=check_link)
                parts.append(b)
                if part:
                    part.add(b, b.seq.astype(np.int64) + sent)
                got += len(b)
                sent += b.n_requested
                if cb:
                    cb(got, n)
                _maybe_move_window(dev, target, got)
                if len(b) == 0 and b.n_timeout >= b.n_requested:
                    raise RuntimeError(
                        "a whole batch of %d exchanges timed out -- the victim "
                        "is not answering" % b.n_requested)
        except BaseException:
            if part:
                part.abandon()
            raise
    else:
        # The probe exchange above CONSUMED `first`. A MODE_BANK victim keeps a
        # replay window, so putting it in the bank sends a sequence number it
        # has already retired and that one exchange comes back rejected -- one
        # short record per capture, small enough to look like noise and
        # therefore worth not having. MODE_FIXED reuses it precisely because
        # its victim does not care.
        try:
            parts = _run_bank(dev, target, it, n, gap_us, check_link, cb, part)
        except BaseException:
            if part:
                part.abandon()
            raise
    if part:
        part.done()

    window = np.concatenate([b.window for b in parts])

    # THE GLOBAL EXCHANGE INDEX. The device's `seq` counts exchanges within one
    # batch and counts the ones that produced no record, so offsetting it by the
    # number of exchanges REQUESTED in the preceding batches -- not by the
    # number of records they returned -- is what makes a record point at the
    # request that actually produced it. Getting this wrong pairs every record
    # with a neighbouring AES input, which does not fail: it quietly yields a
    # capture that verifies nowhere and recovers nothing.
    index, base = [], 0
    for b in parts:
        index.append(b.seq.astype(np.int64) + base)
        base += b.n_requested
    index = np.concatenate(index) if index else np.zeros(0, np.int64)

    return (window,
            np.concatenate([b.dt_hw for b in parts]),
            np.concatenate([b.dt_sw for b in parts]),
            np.concatenate([b.seq for b in parts]),
            dict(win_off=parts[0].win_off, win_len=window.shape[1],
                 clock_hz=parts[0].clock_hz, index=index,
                 n_requested=sum(b.n_requested for b in parts),
                 n_timeout=sum(b.n_timeout for b in parts),
                 n_short=sum(b.n_short for b in parts),
                 n_bad_dt=sum(b.n_bad_dt for b in parts),
                 meta=dict(batches=len(parts))))


def _maybe_move_window(dev, target, got):
    """Let the target move the capture window between batches.

    Cheap when nothing moves -- the default hook returns None without touching
    the device -- and the difference between a capture that is correct across a
    protocol's own length boundary and one that verifies for its first batches
    and nowhere after.
    """
    w = target.between_batches(dev)
    if not w:
        return
    off, length, want = (w if len(w) == 3 else (w[0], w[1], 0))
    cur = dev.config()
    if (cur.win_off, cur.win_len, cur.want) == (off, length, want):
        return
    print("  window moved after %d records: [%d:%d] -> [%d:%d]"
          % (got, cur.win_off, cur.win_off + cur.win_len, off, off + length))
    dev.set_window(off=off, length=length, want=want)


def _run_bank(dev, target, it, n, gap_us, check_link, cb, part=None):
    """MODE_BANK: upload a bank of distinct requests, run it, repeat.

    The bank is played ONCE. Cycling it would re-send a sequence number the
    victim has already seen, and a victim with a replay window answers that with
    a 4.00 rather than the reply this measures -- which arrives as a batch of
    timeouts several minutes into a capture rather than at the first exchange.
    """
    # THE BANK IS BOUNDED TWO WAYS, and which one binds depends on the
    # protocol: OSCORE's 21-byte requests run out of ENTRIES (2 048 of them fit
    # in 43 KB of a 64 KB pool), TLS's 117-byte records run out of BYTES (2 048
    # would be 234 KB). Sizing from the entry limit alone was right for the
    # first protocol tried and wrong for the second, so fill against both.
    info = dev.info()
    # ...and by a THIRD limit that is not the bank's at all: the record ring
    # bounds how many exchanges one run can return, and a bank longer than that
    # has entries the device will never play. It discards them with the bank,
    # so the host has advanced a sequence number past records the victim never
    # saw -- which a TLS session reports as a fatal alert one bank later.
    ring_cap = dev.batch_capacity()
    parts, got, sent = [], 0, 0
    pending = []
    while got < n:
        room = n - got
        used = 0
        batch = []
        while len(batch) < min(room, info.bank_max, ring_cap):
            if not pending:
                pending.append(next(it))
            r = pending[0]
            if used + len(r) + 2 > info.bank_bytes:
                break
            batch.append(pending.pop(0))
            used += len(r) + 2
        if not batch:
            raise RuntimeError(
                "a single request of %d bytes does not fit this board's %d-byte "
                "bank" % (len(pending[0]), info.bank_bytes))
        want = len(batch)
        # `sent` counts exchanges REQUESTED, not records returned. The two
        # differ as soon as one exchange is dropped, and the global index that
        # `blocks()` receives is built from requested counts -- so note_sent
        # must use the same clock or every later bank is announced at the wrong
        # base.
        target.note_sent(sent, batch)
        dev.set_request_bank(batch)
        b = dev.run(want, gap_us=gap_us, check_link=check_link)
        parts.append(b)
        if part:
            part.add(b, b.seq.astype(np.int64) + sent)
        sent += b.n_requested
        got += len(b)
        if cb:
            cb(got, n)
        _maybe_move_window(dev, target, got)
        if len(b) == 0 and b.n_timeout >= want:
            raise RuntimeError(
                "a whole bank of %d requests timed out. For a replay-protected "
                "victim this usually means the sequence numbers went backwards "
                "-- the victim was reset, or the bank was replayed."
                % want)
    return parts


def _run_relay(dev, target, n, progress):
    """MODE_RELAY: one exchange per host round trip, the adapter driving."""
    wins, hw, sw, sq = [], [], [], []
    n_to = 0
    for i in range(n):
        step = target.relay_step(dev, i)
        if step is None:
            n_to += 1
            continue
        w, dt_hw, dt_sw = step
        wins.append(np.frombuffer(bytes(w), np.uint8))
        hw.append(dt_hw)
        sw.append(dt_sw)
        sq.append(i)
        if progress and n > 2000 and (i + 1) % 1000 == 0:
            print("  %s %d/%d" % (target.name, i + 1, n), flush=True)
    if not wins:
        raise RuntimeError("the relay produced no records in %d attempts" % n)
    width = len(wins[0])
    if any(len(w) != width for w in wins):
        raise RuntimeError("relay returned windows of differing length")
    window = np.stack(wins)
    return (window, np.array(hw, np.uint32), np.array(sw, np.uint32),
            np.array(sq, np.uint32),
            dict(win_off=0, win_len=width, clock_hz=dev.info().clock_hz,
                 index=np.array(sq, np.int64),
                 n_requested=n, n_timeout=n_to, n_short=0,
                 meta=dict(batches=0)))


def _check(res: Result, target: Target, spot_check: int):
    """Encrypt the captured inputs under the real key; compare with the outputs."""
    key = target.truth_key()
    if key is None:
        res.verdict = ("NOT CHECKED: this target has no truth key, so nothing "
                       "here distinguishes a correct capture from a window "
                       "landing in the wrong place")
        print("  *** %s ***" % res.verdict)
        return

    # SPREAD THE SAMPLE OVER THE WHOLE CAPTURE, never the first m. A banked
    # capture is uploaded in chunks, and the failure a sample is most likely to
    # meet is an exchange-to-request mapping that is right in the first bank and
    # wrong in every later one. Checking the first 256 records of a 20 000-record
    # capture passes that bug without noticing -- the same shape of mistake as a
    # checker that is green because it is not looking at the thing it was added
    # for. `spot_check=0` checks every block.
    if spot_check and spot_check < res.blocks.n:
        take = np.unique(np.linspace(0, res.blocks.n - 1, spot_check).astype(int))
    else:
        take = np.arange(res.blocks.n)
    ai = res.blocks.aes_in[take].reshape(-1, 16)
    ao = res.blocks.aes_out[take].reshape(-1, 16)
    enc = _aes_ecb(bytes(key), ai)
    good = (enc == ao).all(axis=1)
    ok = int(good.sum())
    res.checked = len(ai)
    res.verified = (ok == len(ai))
    res.verdict = ("E(K, aes_in) == aes_out on %d/%d blocks "
                   "(%d exchanges x %d, spread across the capture)"
                   % (ok, len(ai), len(take), res.blocks.per_exchange))
    print("  " + res.verdict)
    if not res.verified:
        bad_rows = np.where(~good.reshape(len(take), -1).all(axis=1))[0]
        first = int(take[bad_rows[0]])
        raise RuntimeError(
            "ACCEPTANCE FAILED: %s. First failure at record %d of %d "
            "(capture exchange %d), %d of %d sampled exchanges bad.\n"
            "The captured window is not what this victim's AES produced. If "
            "the EARLY records pass and later ones fail, suspect the "
            "exchange-to-request mapping rather than the window; if they all "
            "fail, check the window offset against a fresh reply -- an offset "
            "that was right yesterday moves when a length field in the reply "
            "grows."
            % (res.verdict, first, res.blocks.n,
               int(res.index[first]) if len(res.index) > first else -1,
               len(bad_rows), len(take)))
