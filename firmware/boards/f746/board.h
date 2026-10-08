/* NUCLEO-F746ZG board support for ethtimer.
 *
 * Everything part-specific is behind this header: nothing in `src/` mentions
 * F7 or F4.  The UART register names are the visible difference -- the F7
 * exposes ISR/TDR/RDR where the F4 exposes SR/DR -- and the console is USART3
 * on both, which is what the ST-LINK VCP presents.
 */
#ifndef BOARD_H
#define BOARD_H

#include "main.h"
#include <stdint.h>

#define BOARD_NAME        "NUCLEO-F746ZG"
#define BOARD_ID          746u

struct netif;
extern struct netif gnetif;

void board_init(void);
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

/* Provided by board.c, inherited unchanged from the CubeMX console wiring. */
void uart_write(const uint8_t *p, uint32_t n);
void uart_puts(const char *s);
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
