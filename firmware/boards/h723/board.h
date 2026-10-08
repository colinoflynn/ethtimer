/* NUCLEO-H723ZG board support for ethtimer.
 *
 * Everything part-specific is behind this header: nothing in `src/` mentions
 * a part number.  The H7 shares the F7's UART register flavour (ISR/TDR/RDR)
 * and its console pins (USART3 on PD8/PD9, which is what the ST-LINK VCP
 * presents), so what actually differs from the F7 board is the power supply
 * and clock tree, where the Ethernet DMA is allowed to live, and the three
 * separate ETH clock gates.  See board.c and README.md.
 */
#ifndef BOARD_H
#define BOARD_H

#include "main.h"
#include <stdint.h>

#define BOARD_NAME        "NUCLEO-H723ZG"
#define BOARD_ID          723u

struct netif;
extern struct netif gnetif;

/* Brings up the clock tree, the console, the cycle counter and the
 * network interface.  DWT->CYCCNT IS RUNNING when this returns: it is
 * what this board's Ethernet interrupt latches, so whether it runs cannot
 * be left to an application to remember. */
void board_init(void);

/* The half of board_init() that does not need a TCP/IP stack: HAL, clock tree,
 * console, cycle counter.  An application that drives the Ethernet MAC itself
 * calls this instead of board_init(), and the lwIP half of this file is then
 * dropped by --gc-sections because nothing references it.
 *
 * It exists so that such an application shares THIS clock tree rather than
 * carrying a copy. A reference responder whose clock came from somewhere else
 * would be measuring a different board. */
void board_clock_console_init(void);

/* Called from ETH_IRQHandler with the cycle counter already latched and the DMA
 * status already read, BEFORE any driver work. Return non-zero to say the
 * interrupt is fully handled -- the caller then skips HAL_ETH_IRQHandler, and
 * the hook is responsible for clearing the DMA status bits it consumed.
 *
 * Weak and returning zero by default, so an application that wants the HAL's
 * receive path gets exactly what it got before this existed. It is here for the
 * opposite case: a responder that builds its reply in the interrupt, from a
 * pre-armed descriptor, with no stack between the frame arriving and the frame
 * leaving. `cyc` is the same latch that goes into g_rx_cyc, so a reply can
 * report an interval that starts before any software ran. */
int board_eth_isr_hook(uint32_t cyc, uint32_t dmasr);
void board_tick(void);
/* Drain completed Tx descriptors; called on the TCP path only. */
void board_tx_release(void);
void board_link_tick(void);
void board_netif_set(const uint8_t ip[4], const uint8_t mask[4],
                     const uint8_t gw[4]);
void board_netif_get(uint8_t ip[4], uint8_t mask[4], uint8_t gw[4]);
uint8_t board_link_speed(void);
/* The rate DWT->CYCCNT ticks at, in Hz.  A BOARD question, not an instrument
 * one: on parts where the AHB clock is the core clock these are the same
 * number, and on the H7 they differ by the D1CPRE/HPRE divisions.  Everything
 * the instrument reports as a duration is a count of these ticks, so getting
 * it wrong does not fail -- it reports every exchange uniformly wrong. */
uint32_t board_cyccnt_hz(void);
/* Non-zero if DWT->CYCCNT is actually counting.  board_init() enables it
 * and verifies it by watching it move, because the three register writes
 * that enable it can all be dropped silently when the debug power domain
 * is down -- and a dead counter reports every interval as zero, which is
 * not an error anyone notices. */
int board_dwt_ok(void);
/* Re-enable the cycle counter if it has stopped advancing since the last call.
 * Call it from somewhere that never runs inside a measured exchange, at a few
 * hertz; board_link_tick() already does, and an application that does not call
 * board_link_tick() must call this itself. One register read when all is well. */
void board_dwt_tick(void);

/* Provided by board.c, inherited unchanged from the CubeMX console wiring. */
void uart_write(const uint8_t *p, uint32_t n);
void uart_puts(const char *s);
void uart_putdec(uint32_t v);
void uart_puthex32(uint32_t v);

/* LATCHED BY THE ETHERNET ISR BEFORE ANY DRIVER WORK, and defined in this
 * board's interrupt file -- which is why they are declared here and not in an
 * application's header.  `g_rx_cyc` is the start of any interval an
 * application on this board can honestly report: it is taken before any of
 * that application's code has run.  The `_evt` counters exist so a reader can
 * tell "this timestamp is from the exchange I am looking at" from "this
 * timestamp is stale", which a value alone cannot say. */
extern volatile uint32_t g_tx_cyc, g_rx_cyc, g_tx_evt, g_rx_evt;
int  uart_getchar_nb(void);
/* Bytes the UART receiver lost: ring full, or a hardware overrun. Non-zero
   means a command frame may never have completed. */
uint32_t board_uart_lost(void);

/* DWT is architectural, not part-specific, but the Cortex-M7's DWT block is
 * lock-protected: setting TRCENA alone leaves CYCCNT reading 0 forever.  The
 * unlock write is harmless on the M4, so it lives in the shared start-up path
 * and this macro just says whether it is needed. */
#define BOARD_DWT_NEEDS_UNLOCK  1

#endif /* BOARD_H */
