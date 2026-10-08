/* Copyright 2026 Colin O'Flynn
 * SPDX-License-Identifier: Apache-2.0
 */
/* The reference responder's entry point.
 *
 * The same shape as the instrument's `src/main.c`, and for the same reason:
 * everything part-specific is in `boards/<b>/` and everything
 * application-specific is in one file beside this one. This board support is
 * shared with the instrument **unchanged** -- same clock tree, same console,
 * same lwIP port, same interrupt file -- so a board that can run the
 * instrument can run this.
 *
 * THE LOOP DOES THREE THINGS AND NOT A FOURTH. What is left out matters more
 * than what is in:
 *
 *  * there is no console RECEIVE here. The instrument polls its UART ring for
 *    command frames; this has no commands, so it never reads a byte, and a
 *    stray character on the line cannot land inside an exchange.
 *  * there is no periodic statistics print, no heartbeat and no LED blink. A
 *    UART transmit at 921 600 baud is about 10.8 us per byte with interrupts
 *    enabled, and a forty-byte status line dropped into the loop every second
 *    would appear as a 430 us outlier once per second in somebody's jitter
 *    histogram. `etv_poll()` prints only when a request asked it to.
 *  * there is nothing on a timer at all. `sys_check_timeouts()` inside
 *    `board_tick()` is lwIP's own, and on this configuration it has ARP
 *    expiry and nothing else to do.
 */
#include "board.h"
#include "et_victim.h"

int main(void)
{
    board_init();
    etv_init();

    for (;;)
    {
        /* The stack, first and always: the reply is sent from inside this call,
         * so anything that delays it is added to the interval the responder
         * reports -- which is honest, but it is still delay. */
        board_tick();

        /* Rate-limited inside board_link_tick() because it costs MDIO
         * transactions, which are tens of microseconds and must not sit in the
         * middle of an exchange. */
        board_link_tick();

        etv_poll();
    }
}

#ifdef USE_FULL_ASSERT
void assert_failed(uint8_t *file, uint32_t line) { (void)file; (void)line; while (1) {} }
#endif
