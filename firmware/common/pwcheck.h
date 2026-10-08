/* Copyright 2026 Colin O'Flynn
 * SPDX-License-Identifier: Apache-2.0
 */
/* A password check that leaks its answer through its own running time, and one
 * that does not. Compiled into both reference responders.
 *
 * WHAT THIS IS FOR. `ethtimer` measures how long a device takes to answer. This
 * gives it something whose answer time depends on a secret, so that the
 * measurement has something to find -- and, next to it, the same check written
 * the way it should be, so that a flat result proves the measurement was
 * sensitive enough to have seen a difference if there were one.
 *
 * Both halves matter. A demo that only shows the leak cannot tell "the
 * comparison is constant-time" from "my instrument is too noisy", and those are
 * very different conclusions to draw about somebody's firmware.
 *
 * THE LEAK IS NOT SUBTLE AND IS NOT MEANT TO BE. `pw_check_early` returns at the
 * first wrong byte, so its running time grows with the number of leading bytes
 * the caller got right. That is what `strcmp`, `memcmp` and every hand-written
 * `for` loop over a secret do, and it is why constant-time comparison exists.
 * Measured on a NUCLEO-F429ZI at 180 MHz the step is about 30 ns per byte --
 * five cycles of the counter -- which is nothing at all next to a network, and
 * trivially recoverable once averaged.
 *
 * The secret is a compile-time constant and the host demo knows it, because the
 * point is to check that a recovered answer is right, not to keep one.
 */
#ifndef PWCHECK_H
#define PWCHECK_H

#include <stdint.h>

/* Override at build time:  EXTRA_DEFS='-DETV_PASSWORD="\"swordfish\""' */
#ifndef ETV_PASSWORD
#define ETV_PASSWORD "hunter2!"
#endif

/* Longest guess a request may carry. Bounded so a malformed length cannot walk
 * off the end of the frame. */
#define PW_MAX_GUESS  32u

/* HOW MANY TIMES THE COMPARISON RUNS PER REQUEST.
 *
 * One, by default, which is what real code does and what the demo measures.
 * ONE PASS IS ENOUGH: on a NUCLEO-F429ZI the step is 11 cycles, 61 ns, per
 * correct byte, and demos/password recovers an eight-byte secret from it in
 * under a minute -- both from the responder's own clock and from the round trip
 * alone, which is what a remote attacker has.
 *
 * (An earlier version of this comment claimed the round trip could not resolve a
 * single pass. That was wrong, and wrong for a reason worth more than the claim:
 * the responder was printing a status line once a second, and a group of
 * exchanges is short enough to land entirely inside one print. The diagnostic
 * was perturbing the measurement. The print is on request only now, and the
 * remote scan works at one pass.)
 *
 * Raising it makes the per-byte cost larger, which is useful for two honest
 * reasons rather than for flattering the demo: it stands in for a victim whose
 * comparison costs more per byte -- a longer secret, a hash compared bytewise,
 * an interpreted language, a cache miss per element -- and it widens the thinner
 * margins. At one pass some positions separate by only a few nanoseconds on the
 * round trip; at eight passes the step is about 490 ns and every position is
 * clear by a wide margin.
 *
 * It is an amplifier, it is labelled as one, and the single-pass numbers are the
 * ones to quote.
 */
#ifndef ETV_PW_ROUNDS
#define ETV_PW_ROUNDS 1u
#endif

/* How many bytes the secret is. Computed from the literal, so changing the
 * password changes nothing else. */
uint32_t pw_len(void);

/* THE LEAKY ONE: returns at the first byte that differs.
 *
 * 1 if the guess matches the secret exactly, 0 otherwise -- one bit, which is
 * all a real login gives back. Everything else it tells you is in how long it
 * took.
 */
int pw_check_early(const uint8_t *guess, uint32_t n);

/* THE CONTROL: looks at every byte whatever happens, and folds the differences
 * together so there is no branch on the secret at all.
 *
 * Same one bit of result. Its running time depends on the LENGTH of the guess,
 * which is public, and on nothing else. A capture against this one should come
 * out flat; if it does not, the thing to doubt is the measurement.
 */
int pw_check_const(const uint8_t *guess, uint32_t n);

#endif /* PWCHECK_H */
