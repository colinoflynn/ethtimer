/* Copyright 2026 Colin O'Flynn
 * SPDX-License-Identifier: Apache-2.0
 */
/* ethtimer -- entry point.
 *
 * Deliberately almost empty: everything part-specific is in boards/<b>/board.c
 * and everything instrument-specific is in src/ethtimer.c.  The loop does the
 * three things that must keep happening and nothing else.
 */
#include "board.h"
#include "ethtimer.h"

int main(void)
{
    board_init();
    et_init();

    for (;;)
    {
        /* Stack first: a reply that is not pumped in is a timeout, and a
         * timeout inside a batch is indistinguishable from a slow victim. */
        board_tick();

        /* The PHY read costs MDIO transactions, so it is rate-limited inside
         * board_link_tick() and never runs inside a measured exchange. */
        board_link_tick();

        et_poll();
    }
}

#ifdef USE_FULL_ASSERT
void assert_failed(uint8_t *file, uint32_t line) { (void)file; (void)line; while (1) {} }
#endif
