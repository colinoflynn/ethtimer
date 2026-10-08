/**
  ******************************************************************************
  * @file    board.c
  * @brief   NUCLEO-H723ZG board support for ethtimer: clock tree, console,
  *          PHY and netif. Nothing in src/ mentions a part number.
  ******************************************************************************
  * The netif and PHY bring-up is the F746 board's, unchanged: the H7 and the F7
  * share the same generation of HAL ETH driver, so the Ethernet code is the
  * same code. Derived from STM32Cube's NUCLEO LwIP applications with LwIP in
  * NO_SYS=1 raw-API mode. Copyright (c) 2016 STMicroelectronics.
  *
  * WHAT IS ACTUALLY DIFFERENT ON THIS PART is below: the core supply, the
  * voltage scaling, a three-stage PLL, and the fact that the Ethernet DMA
  * cannot see all of RAM. See SystemClock_Config, and the linker script.
  *
  * THE L1 D-CACHE IS LEFT OFF on this board.  An instrument gains nothing from
  * it -- it spends its time waiting on a wire -- and turning it off removes
  * every DMA-coherency carve-out, so the ETH descriptors and buffers are
  * ordinary memory that merely has to be in the right bus domain.  On an H7
  * with the D-cache on, every descriptor touch needs invalidate/clean around
  * it, and the failure when one is missed is a frame that arrives and is
  * processed from a stale cache line -- which is a corrupt capture, not a
  * crash.  Not worth it for a board that spends its time waiting on a wire.
  ******************************************************************************
  */
#include "main.h"
#include <string.h>
#include "ethernetif.h"
#include "lwip/init.h"
#include "lwip/netif.h"
#include "lwip/timeouts.h"
#include "lwip/etharp.h"
#include "board.h"
#include "lan8742.h"

struct netif gnetif;
static UART_HandleTypeDef huart3;

static void SystemClock_Config(void);
static void Netif_Config(void);
static void Console_Config(void);

/* ---- ethtimer board support ------------------------------------------- *
 *
 * Generated from this board's CubeMX `main.c` by scratchpad/mk_board.py and then
 * maintained by hand.  What is board-specific lives here and nowhere else: the
 * clock tree, the console pins, and the UART register flavour (F7 exposes
 * ISR/TDR).  `src/` must not mention a part number at all.
 */


/* THE CYCLE COUNTER IS ENABLED HERE, not in an application.
 *
 * `g_rx_cyc` and `g_tx_cyc` are latched by THIS board's Ethernet interrupt, so
 * the counter they read has to be running before any application does
 * anything -- and whether it is running must not depend on which application
 * was linked. It used to: the instrument enabled it in its own init, and the
 * reference responder, sharing this same board support, read zeroes and
 * reported every interval as 0.000 us. Not an error, and not a number anyone
 * would look at twice.
 *
 * Idempotent, so an application that also enables it is no worse off. */
static uint8_t dwt_running;

/* Enable the cycle counter AND CHECK THAT IT COUNTS, retrying if it does not.
 *
 * The check is not paranoia. Enabling the counter is three register writes and
 * they can all appear to succeed while the counter stays dead: the DWT lives in
 * the debug power domain, and on this family a write to DEMCR or DWT->CTRL that
 * lands while that domain is not powered is dropped with no indication. Whether
 * it is powered at the instant start-up code runs depends on what a debugger
 * did last, so the failure is INTERMITTENT -- it survives one reset and not the
 * next.
 *
 * Measured here: the reference responder reported its own interval as exactly
 * 0.000 us on all 20 000 exchanges of one capture and correct values on the
 * next, with the same binary. A dead counter does not read as an error. It
 * reads as an endpoint that contributes nothing, which is a path figure with
 * the whole round trip in it -- about 190 us where the truth was 21.
 *
 * So: write, then prove it by watching the counter move, and try again if it
 * did not. `board_dwt_ok()` lets the application say so out loud instead of
 * reporting a constant. */
static void dwt_enable(void)
{
  int tries;

  for (tries = 0; tries < 8; tries++)
  {
    uint32_t a;
    volatile int spin;

    CoreDebug->DEMCR |= CoreDebug_DEMCR_TRCENA_Msk;
#if BOARD_DWT_NEEDS_UNLOCK
    /* The Cortex-M7's DWT is lock-protected; without this CYCCNT reads 0
     * forever and every measurement silently becomes a constant. */
    DWT->LAR = 0xC5ACCE55u;
#endif
    DWT->CTRL |= DWT_CTRL_CYCCNTENA_Msk;

    /* Not DWT->CYCCNT = 0: zeroing it buys nothing -- every consumer takes a
     * difference, and the 32-bit wrap has to be handled regardless. */
    a = DWT->CYCCNT;
    for (spin = 0; spin < 64; spin++) { }
    if (DWT->CYCCNT != a) { dwt_running = 1u; return; }
  }
  dwt_running = 0u;
}

/* Non-zero if DWT->CYCCNT is counting.  An application that reports timing
 * should check this once and say so if it is zero, because every interval it
 * publishes afterwards would be zero and nothing else would mark them. */
int board_dwt_ok(void) { return (int)dwt_running; }

/* HAL, clock tree, console, cycle counter.  Split out of board_init() so
 * that an application which drives the MAC itself can share this board's
 * clock tree without pulling in a TCP/IP stack -- see board.h. */
void board_clock_console_init(void)
{
  SCB_EnableICache();

  HAL_Init();
  SystemClock_Config();

  /* THE D2 SRAM IS CLOCK-GATED OFF AT RESET on this family, and the Ethernet
     descriptor rings live there (see the linker script).  Writing to an
     unclocked SRAM does not read back as zero and does not fault at the
     offending instruction: it raises an IMPRECISE bus fault, which escalates
     to a HardFault at whatever the core happened to reach next.
     Measured here before this line existed: CFSR = 0x00000400 (IMPRECISERR),
     HFSR = 0x40000000 (FORCED), BFAR invalid, the stacked PC useless -- a
     board that clocks correctly, configures its UART correctly, and then sits
     silently in a fault handler, which reads as a bad flash.

     Both halves are enabled: the descriptors are in the first 16 KB, but the
     two are one contiguous region as far as the linker script is concerned and
     a ring that grows past 0x30004000 must not fall off a clock boundary. */
  __HAL_RCC_D2SRAM1_CLK_ENABLE();
  __HAL_RCC_D2SRAM2_CLK_ENABLE();

  Console_Config();

  /* AFTER the clock tree and the console, not before.  Every clock and
   * power change is done by now, and a failure here can be printed. */
  dwt_enable();
}

void board_init(void)
{
  board_clock_console_init();

  lwip_init();
  Netif_Config();
}

/* Run the stack.  Called from the main loop and from inside the capture loop's
 * wait, so it must never block. */
void board_tick(void)
{
  if (RxPktPending) { ethernetif_input(&gnetif); }
  sys_check_timeouts();
}

/* Drain completed transmit descriptors.

   ST's NO_SYS lwIP port releases them only when HAL_ETH_Transmit_IT reports the
   ring full, so one send in every ETH_TX_DESC_CNT pays for a descriptor sweep
   and a frame-time of waiting -- a second timing mode ~1 400 cycles slower
   holding one exchange in ETH_TX_DESC_CNT.  It is independent of anything being
   measured, but on the TCP path it lands between the record and the reply, so
   the relay clears it before every measured send.

   The UDP capture path deliberately does NOT call this: its published results
   were taken without it, the artefact is removed in analysis instead, and
   changing the measured path would make this a different instrument rather than
   the same one. */
void board_tx_release(void)
{
  extern ETH_HandleTypeDef EthHandle;
  HAL_ETH_ReleaseTxPacket(&EthHandle);
  TxPktDone = 0;
}

/* The link watchdog is separate from board_tick(): it costs a PHY register read
 * over MDIO, which is far too slow to sit inside a measured exchange. */

/* RE-ENABLE THE CYCLE COUNTER IF IT HAS STOPPED ADVANCING.
 *
 * The DWT is in the debug power domain, and the counter can freeze while every
 * register still reads as though it were running. Measured here: it held 484 ms
 * of uptime indefinitely with DWT->CTRL reading 0x40000001 -- CYCCNTENA set --
 * so the Ethernet ISR latched a constant and every interval computed from it
 * came out as exactly zero.
 *
 * THE TEST IS WHETHER IT MOVED, not whether its enable bit is set. The first
 * version of this checked CYCCNTENA and therefore never fired, which is the
 * same mistake as trusting a status register over an observation.
 *
 * Exactly zero is the dangerous part: it is not an error, it is an endpoint
 * that appears to contribute nothing, and a path figure then silently contains
 * the whole round trip.
 *
 * CALL IT FROM SOMEWHERE THAT NEVER RUNS INSIDE A MEASURED EXCHANGE, at a few
 * hertz. It is its own function rather than part of board_link_tick() because
 * an application that drives the MAC itself does not call that one -- and the
 * first such application spent an afternoon reporting zeroes for exactly this
 * reason. Any interval shorter than a wrap works: 100 ms is millions of cycles
 * at any clock this part runs, so an unchanged value means stopped.
 *
 * Cheap: one register read when all is well. */
void board_dwt_tick(void)
{
  static uint32_t last_cyc;
  uint32_t now_cyc = DWT->CYCCNT;

  if (now_cyc == last_cyc)
  {
    dwt_enable();
    now_cyc = DWT->CYCCNT;
  }
  last_cyc = now_cyc;
}

void board_link_tick(void)
{
  static uint32_t last_link = 0;
  uint32_t now = HAL_GetTick();
  if ((now - last_link) >= 100U)
  {
    last_link = now;
    ethernet_link_check_state(&gnetif);
    ethernetif_rmii_watchdog();
    board_dwt_tick();
  }
}

/* Re-address the interface at run time.  The firmware this replaces held the
 * instrument's own address AND the measured device's as compile-time
 * constants, so moving to another subnet meant editing a header, rebuilding
 * and reflashing.  lwIP's netif_set_addr is safe to call after bring-up; the
 * ARP cache is flushed so a stale entry for the old subnet cannot answer. */
/* The negotiated PHY speed, as an ET_LINK_* code.  This is board support and
 * not instrument logic because the PHY driver is a board choice -- both Nucleo
 * 144 boards here carry a LAN8742, but nothing in src/ should assume that.
 *
 * It reports SPEED, never just up/down: a 10 Mbit half-duplex link serialises a
 * frame ten times more slowly, which quantises the arrival instant ten times
 * more coarsely and erases the structure this instrument measures.  It reads as
 * link-up exactly like 100 Mbit full, and was blamed on three different
 * Ethernet switches before the round-trip time gave it away.
 */
uint8_t board_link_speed(void)
{
  extern lan8742_Object_t LAN8742;
  int32_t st;
  if (!netif_is_link_up(&gnetif)) { return 0u; }        /* ET_LINK_DOWN */
  st = LAN8742_GetLinkState(&LAN8742);
  if (st == LAN8742_STATUS_100MBITS_FULLDUPLEX) { return 4u; }
  if (st == LAN8742_STATUS_100MBITS_HALFDUPLEX) { return 3u; }
  if (st == LAN8742_STATUS_10MBITS_FULLDUPLEX)  { return 2u; }
  if (st == LAN8742_STATUS_10MBITS_HALFDUPLEX)  { return 1u; }
  return 0u;
}


/* NOT HAL_RCC_GetHCLKFreq() on this part.  That returns the AHB clock, which
 * here is the core clock divided by HPRE -- 200 MHz against the core's 400 --
 * and DWT->CYCCNT counts CORE cycles.  Reporting the AHB clock would have made
 * every measured turnaround read twice its real length, uniformly, with
 * nothing in the output to say so.
 *
 * HAL_RCC_GetHCLKFreq() is called for its side effect: it is what refreshes
 * SystemCoreClock from the live RCC registers.  SystemCoreClock alone would
 * also be right, but only because HAL_RCC_ClockConfig happened to set it, and
 * that is a fact about initialisation order rather than about the clock. */
uint32_t board_cyccnt_hz(void)
{
  (void)HAL_RCC_GetHCLKFreq();
  return SystemCoreClock;
}

void board_netif_set(const uint8_t ip[4], const uint8_t mask[4],
                     const uint8_t gw[4])
{
  ip4_addr_t a, m, g;
  IP4_ADDR(&a, ip[0], ip[1], ip[2], ip[3]);
  IP4_ADDR(&m, mask[0], mask[1], mask[2], mask[3]);
  IP4_ADDR(&g, gw[0], gw[1], gw[2], gw[3]);
  netif_set_addr(&gnetif, &a, &m, &g);
  etharp_cleanup_netif(&gnetif);
}

void board_netif_get(uint8_t ip[4], uint8_t mask[4], uint8_t gw[4])
{
  uint32_t a = ip4_addr_get_u32(netif_ip4_addr(&gnetif));
  uint32_t m = ip4_addr_get_u32(netif_ip4_netmask(&gnetif));
  uint32_t g = ip4_addr_get_u32(netif_ip4_gw(&gnetif));
  int i;
  for (i = 0; i < 4; i++)
  {
    ip[i]   = (uint8_t)(a >> (8 * i));
    mask[i] = (uint8_t)(m >> (8 * i));
    gw[i]   = (uint8_t)(g >> (8 * i));
  }
}

static void Netif_Config(void)
{
  ip4_addr_t ipaddr, netmask, gw;

  IP4_ADDR(&ipaddr,  IP_ADDR0,      IP_ADDR1,      IP_ADDR2,      IP_ADDR3);
  IP4_ADDR(&netmask, NETMASK_ADDR0, NETMASK_ADDR1, NETMASK_ADDR2, NETMASK_ADDR3);
  IP4_ADDR(&gw,      GW_ADDR0,      GW_ADDR1,      GW_ADDR2,      GW_ADDR3);

  netif_add(&gnetif, &ipaddr, &netmask, &gw, NULL, &ethernetif_init, &netif_input);
  netif_set_default(&gnetif);

  if (netif_is_link_up(&gnetif)) { netif_set_up(&gnetif); }
  else                           { netif_set_down(&gnetif); }
}

/** HSI 64 MHz -> PLL1 -> 400 MHz CPU, 200 MHz AHB, 100 MHz APB.
 *
 * HSI RATHER THAN THE ST-LINK's 8 MHz MCO, deliberately. The MCO reaches the
 * MCU through a solder-bridge configuration that differs between Nucleo
 * revisions, and an HSE that is not actually connected does not fail visibly:
 * HAL_RCC_OscConfig spins waiting for HSERDY and the board never prints
 * anything, which reads as a bad flash rather than a clock. HSI is on at reset
 * on every one of these parts.
 *
 * Nothing the instrument does needs a crystal's accuracy. The 50 MHz RMII
 * reference clock comes from the PHY, not from here, so Ethernet timing is
 * unaffected; the console's baud rate is the only thing HSI's +/-1 % touches,
 * and 921 600 from a 100 MHz APB divides to 108.5 -- about 0.5 % of rounding on
 * top, well inside what 8N1 framing tolerates. AND IT DOES NOT AFFECT A
 * MEASUREMENT: `dt_hw` is a count of THIS clock's cycles and GET_INFO reports
 * the frequency the host should divide by, so a board running 0.5 % fast
 * reports microseconds 0.5 % short, uniformly, with no effect on the structure
 * of the distribution. If absolute microseconds to better than a percent ever
 * matter here, that is the moment to find the crystal -- not before.
 *
 * THE ARITHMETIC. HSI 64 MHz / DIVM1 16 = 4 MHz reference (VCIRANGE_2 covers
 * 4-8 MHz), x DIVN1 200 = 800 MHz VCO (WIDE covers 192-836 MHz), / DIVP1 2 =
 * 400 MHz. D1CPRE 1 leaves the CPU at 400; HPRE 2 puts AHB at 200 and every
 * APB at 100.
 *
 * 400 rather than this part's maximum 550: 550 needs voltage scale 0 and the
 * SYSCFG over-drive sequence, and buys an instrument that spends its time
 * waiting on a wire precisely nothing. Four wait states on the flash where the
 * table allows fewer, for the same reason -- extra wait states cannot be wrong,
 * and a clock change that silently needs one more is a part that hard-faults
 * at a random instruction.
 */
static void SystemClock_Config(void)
{
  RCC_OscInitTypeDef osc = {0};
  RCC_ClkInitTypeDef clk = {0};

  /* THE SUPPLY CONFIGURATION COMES FIRST, and getting it wrong is the one
     failure here that looks like a dead board: the voltage-scaling write
     below never reports ready, HAL spins, and nothing is ever printed.

     THE LDO, not the SMPS. Several H7 lines have a switched-mode core
     supply and the H72x/H73x do not -- PWR_DIRECT_SMPS_SUPPLY is not even
     declared for this part, which is a better error than the one an H743
     example copied verbatim would have given here. */
  if (HAL_PWREx_ConfigSupply(PWR_LDO_SUPPLY) != HAL_OK) { while (1) {} }

  __HAL_PWR_VOLTAGESCALING_CONFIG(PWR_REGULATOR_VOLTAGE_SCALE1);
  while (!__HAL_PWR_GET_FLAG(PWR_FLAG_VOSRDY)) { }

  osc.OscillatorType      = RCC_OSCILLATORTYPE_HSI;
  osc.HSIState            = RCC_HSI_DIV1;        /* 64 MHz, not 64/2 */
  osc.HSICalibrationValue = RCC_HSICALIBRATION_DEFAULT;
  osc.PLL.PLLState        = RCC_PLL_ON;
  osc.PLL.PLLSource       = RCC_PLLSOURCE_HSI;
  osc.PLL.PLLM            = 16;                  /* 64 / 16 = 4 MHz  */
  osc.PLL.PLLN            = 200;                 /* 4 * 200 = 800 MHz VCO */
  osc.PLL.PLLP            = 2;                   /* 800 / 2 = 400 MHz */
  osc.PLL.PLLQ            = 4;
  osc.PLL.PLLR            = 2;
  osc.PLL.PLLRGE          = RCC_PLL1VCIRANGE_2;  /* reference is 4-8 MHz */
  osc.PLL.PLLVCOSEL       = RCC_PLL1VCOWIDE;     /* VCO is 192-836 MHz  */
  osc.PLL.PLLFRACN        = 0;
  if (HAL_RCC_OscConfig(&osc) != HAL_OK) { while (1) {} }

  clk.ClockType      = (RCC_CLOCKTYPE_SYSCLK | RCC_CLOCKTYPE_HCLK |
                        RCC_CLOCKTYPE_D1PCLK1 | RCC_CLOCKTYPE_PCLK1 |
                        RCC_CLOCKTYPE_PCLK2   | RCC_CLOCKTYPE_D3PCLK1);
  clk.SYSCLKSource   = RCC_SYSCLKSOURCE_PLLCLK;
  clk.SYSCLKDivider  = RCC_SYSCLK_DIV1;          /* CPU 400 MHz */
  clk.AHBCLKDivider  = RCC_HCLK_DIV2;            /* AHB 200 MHz */
  clk.APB3CLKDivider = RCC_APB3_DIV2;            /* 100 MHz     */
  clk.APB1CLKDivider = RCC_APB1_DIV2;
  clk.APB2CLKDivider = RCC_APB2_DIV2;
  clk.APB4CLKDivider = RCC_APB4_DIV2;
  if (HAL_RCC_ClockConfig(&clk, FLASH_LATENCY_4) != HAL_OK) { while (1) {} }
}

/* --------------------------------------------------------------------------
 * ST-LINK VCP console (USART3, PD8/PD9). This is the instrument's only link to
 * the host PC: control commands in, trace records out.
 * ----------------------------------------------------------------------- */
/* ---------------------------------------------------------------- UART RX --
 * Interrupt-driven, with a ring, because polling it from the main loop DROPS
 * BYTES.  At 921 600 baud a byte lands every 10.8 us; `board_tick()` can spend
 * far longer than that inside `ethernetif_input()`, and the PHY read in
 * `board_link_tick()` is tens of microseconds of MDIO.  Short commands
 * survived that -- a 129-byte SET_REQUEST is 1.4 ms of wire and the loop gets
 * round often enough -- so the fault only appeared when v2's SET_BANK started
 * sending 2 KB frames, where it showed up as a command that was simply never
 * answered.
 *
 * The ring holds more than one maximum frame, so a whole frame can arrive
 * while the loop is busy elsewhere.  An overrun is COUNTED rather than
 * swallowed: a lost byte means the frame never completes and the host waits
 * for an ACK that cannot come, which is precisely the kind of silence that is
 * expensive to diagnose from the other end.
 *
 * The priority is deliberately NUMERICALLY HIGHER than ETH_IRQn's 7, i.e.
 * lower urgency, so this can never delay the Ethernet ISR that latches
 * DWT->CYCCNT.  That latch is the measurement; nothing may preempt it. */
#define UART_RX_RING  4096u
static volatile uint8_t  uart_rx_buf[UART_RX_RING];
static volatile uint32_t uart_rx_head, uart_rx_tail;
static volatile uint32_t uart_rx_lost;

uint32_t board_uart_lost(void) { return uart_rx_lost; }

static void uart_rx_push(uint8_t b)
{
  uint32_t h = uart_rx_head, n = (h + 1u) % UART_RX_RING;
  if (n == uart_rx_tail) { uart_rx_lost++; return; }   /* ring full */
  uart_rx_buf[h] = b;
  uart_rx_head = n;
}

static void Console_Config(void)
{
  GPIO_InitTypeDef g = {0};

  __HAL_RCC_GPIOD_CLK_ENABLE();
  __HAL_RCC_USART3_CLK_ENABLE();

  g.Pin = GPIO_PIN_8 | GPIO_PIN_9;
  g.Mode = GPIO_MODE_AF_PP;
  g.Pull = GPIO_PULLUP;
  g.Speed = GPIO_SPEED_FREQ_VERY_HIGH;
  g.Alternate = GPIO_AF7_USART3;
  HAL_GPIO_Init(GPIOD, &g);

  huart3.Instance = USART3;
  huart3.Init.BaudRate = CONSOLE_BAUD;
  huart3.Init.WordLength = UART_WORDLENGTH_8B;
  huart3.Init.StopBits = UART_STOPBITS_1;
  huart3.Init.Parity = UART_PARITY_NONE;
  huart3.Init.Mode = UART_MODE_TX_RX;
  huart3.Init.HwFlowCtl = UART_HWCONTROL_NONE;
  huart3.Init.OverSampling = UART_OVERSAMPLING_16;
  HAL_UART_Init(&huart3);

  /* Receive on the interrupt, not from the main loop.  See the UART RX ring
     above for why polling drops bytes. */
  uart_rx_head = uart_rx_tail = uart_rx_lost = 0;
  USART3->CR1 |= USART_CR1_RXNEIE;
  HAL_NVIC_SetPriority(USART3_IRQn, 10, 0);
  HAL_NVIC_EnableIRQ(USART3_IRQn);
}

void USART3_IRQHandler(void)
{
  uint32_t isr = USART3->ISR;
  /* The H7's USART has a FIFO, so the flags are named for both the register
     and the FIFO: RXNE_RXFNE, not RXNE. The FIFO itself is left disabled --
     the receiver is interrupt-driven into a 4 KB ring already, and a FIFO
     would only move where the bytes queue. */
  if (isr & USART_ISR_RXNE_RXFNE) { uart_rx_push((uint8_t)(USART3->RDR & 0xFF)); }
  if (isr & (USART_ISR_ORE | USART_ISR_FE | USART_ISR_NE))
  {
    uart_rx_lost++;
    USART3->ICR = USART_ICR_ORECF | USART_ICR_FECF | USART_ICR_NECF;
    /* Read the register back after clearing it.  On this M7 a handler short
       enough to return before the write has reached the peripheral is
       re-entered immediately, and the core spins in the ISR forever. */
    (void)USART3->ISR;
  }
}

void uart_write(const uint8_t *p, uint32_t n)
{
  /* Direct register poll: HAL_UART_Transmit's per-call overhead is large next
     to a 20-byte trace record. */
  while (n--)
  {
    while (!(USART3->ISR & USART_ISR_TXE_TXFNF)) { }
    USART3->TDR = *p++;
  }
}

void uart_puts(const char *s) { uart_write((const uint8_t *)s, (uint32_t)strlen(s)); }

int uart_getchar_nb(void)
{
  uint32_t t = uart_rx_tail;
  int c;
  if (t == uart_rx_head) { return -1; }
  c = (int)uart_rx_buf[t];
  uart_rx_tail = (t + 1u) % UART_RX_RING;
  return c;
}

void uart_puthex32(uint32_t v)
{
  static const char HEX[] = "0123456789abcdef";
  char b[8];
  int i;
  for (i = 0; i < 8; i++) { b[i] = HEX[(v >> (28 - 4 * i)) & 0xF]; }
  uart_write((const uint8_t *)b, 8);
}

void uart_putdec(uint32_t v)
{
  char b[12], o[12];
  int n = 0, i;
  if (!v) { uart_puts("0"); return; }
  while (v) { b[n++] = (char)('0' + v % 10); v /= 10; }
  for (i = 0; i < n; i++) { o[i] = b[n - 1 - i]; }
  uart_write((const uint8_t *)o, (uint32_t)n);
}

#ifdef  USE_FULL_ASSERT
void assert_failed(uint8_t *file, uint32_t line) { (void)file; (void)line; while (1) {} }
#endif
