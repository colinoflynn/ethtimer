/* Copyright 2026 Colin O'Flynn
 * SPDX-License-Identifier: Apache-2.0
 */
/* See pwcheck.h for what this is for. */
#include "pwcheck.h"

static const char  PW[] = ETV_PASSWORD;
/* sizeof includes the terminator, which is not part of the secret. */
#define PW_LEN  ((uint32_t)(sizeof(PW) - 1u))

uint32_t pw_len(void) { return PW_LEN; }

/* WHY THE POINTERS ARE VOLATILE.
 *
 * Without it, GCC at -O2 is entitled to recognise this loop and replace it with
 * a call to `memcmp`, or to unroll it and compare four bytes at a time. Either
 * is a perfectly good compare and neither is the thing the demo is about: the
 * timing profile would then be per-word, or per-libc-implementation, and the
 * source would no longer say what is being measured.
 *
 * `volatile` makes every byte a real load in program order, so the loop that
 * runs is the loop that is written. It is also, for once, honest about the
 * target: this is exactly the shape of a hand-written comparison in real
 * firmware, and newlib's own `strcmp` leaks the same way for the same reason.
 */
int pw_check_early(const uint8_t *guess, uint32_t n)
{
    volatile const uint8_t *g = guess;
    volatile const uint8_t *s = (volatile const uint8_t *)PW;
    uint32_t i;
    uint32_t round;
    int result = 0;

    /* THE LENGTH IS ITS OWN LEAK, and it is a bigger one than the bytes: a
     * wrong length is rejected before a single byte is looked at, so the
     * fastest possible answer tells an attacker the length is wrong. Left in,
     * because real code does this and a demo that quietly fixed it would be
     * showing a smaller leak than the one it is about. */
    if (n != PW_LEN)
    {
        return 0;
    }

    /* The outer loop is the amplifier (ETV_PW_ROUNDS, 1 by default). Each pass
     * early-returns in its own right, so R passes cost R times as much as one
     * and the per-byte step scales with R. `result` rather than an early return
     * out of the outer loop, so every round actually runs. */
    for (round = 0; round < ETV_PW_ROUNDS; round++)
    {
        int ok = 1;

        for (i = 0; i < PW_LEN; i++)
        {
            if (g[i] != s[i])
            {
                ok = 0;     /* <-- the whole point: it stops here */
                break;
            }
        }
        result = ok;
    }
    return result;
}

int pw_check_const(const uint8_t *guess, uint32_t n)
{
    volatile const uint8_t *g = guess;
    volatile const uint8_t *s = (volatile const uint8_t *)PW;
    uint8_t  diff = 0u;
    uint32_t i;

    /* Still rejects a wrong length -- it has to, and the length is public
     * anyway -- but it does so without touching the secret, and the loop below
     * then runs over a fixed count. */
    if (n != PW_LEN)
    {
        return 0;
    }

    /* No branch on the secret, no early exit, every byte read exactly once.
     * The differences are OR-ed together, so the result depends on all of them
     * and the time depends on none of them.
     *
     * The same number of rounds as the leaky one, so a comparison between the
     * two is a comparison of the comparisons and not of how often they ran. */
    {
        uint32_t round;
        for (round = 0; round < ETV_PW_ROUNDS; round++)
        {
            for (i = 0; i < PW_LEN; i++)
            {
                diff |= (uint8_t)(g[i] ^ s[i]);
            }
        }
    }

    /* And the answer is folded without a branch either: `diff == 0` compiles to
     * a flag test, not to a jump whose direction depends on the secret. */
    return (diff == 0u) ? 1 : 0;
}
