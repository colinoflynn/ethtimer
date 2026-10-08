/**
  ******************************************************************************
  * @file    board.c
  * @brief   NUCLEO-F429ZI board support for ethtimer: clock tree, console,
  *          PHY and netif. Nothing in src/ mentions a part number.
  ******************************************************************************
  * Clock tree, ETH/LwIP bring-up and HAL usage are derived from STM32Cube's
  * NUCLEO LwIP applications, with LwIP in NO_SYS=1 raw-API mode.
  * Copyright (c) 2016 STMicroelectronics.
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
 * ISR/TDR, F4 exposes SR/DR).  `src/` must not mention either part number.
 */

void board_init(void)
{

  HAL_Init();
  SystemClock_Config();
  Console_Config();

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
void board_link_tick(void)
{
  static uint32_t last_link = 0;
  uint32_t now = HAL_GetTick();
  if ((now - last_link) >= 100U)
  {
    last_link = now;
    ethernet_link_check_state(&gnetif);
    ethernetif_rmii_watchdog();
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

/** HSE 8 MHz (ST-LINK MCO bypass) -> PLL -> 180 MHz, 5 wait states.
 *
 * PLLM 8, PLLN 360, PLLP 2: 8/8 * 360 / 2 = 180 MHz, with the over-drive
 * regulator on, which this part needs above 168.  The instrument's clock is
 * reported in GET_INFO and carried in every BATCH_HDR, so a host turns
 * cycles into microseconds from what the board says rather than from a
 * constant -- which is what lets this board and the 216 MHz one agree on a
 * median to well under a microsecond. */
static void SystemClock_Config(void)
{
  RCC_ClkInitTypeDef RCC_ClkInitStruct;
  RCC_OscInitTypeDef RCC_OscInitStruct;

  __HAL_RCC_PWR_CLK_ENABLE();
  __HAL_PWR_VOLTAGESCALING_CONFIG(PWR_REGULATOR_VOLTAGE_SCALE1);

  RCC_OscInitStruct.OscillatorType = RCC_OSCILLATORTYPE_HSE;
  RCC_OscInitStruct.HSEState = RCC_HSE_BYPASS;
  RCC_OscInitStruct.PLL.PLLState = RCC_PLL_ON;
  RCC_OscInitStruct.PLL.PLLSource = RCC_PLLSOURCE_HSE;
  RCC_OscInitStruct.PLL.PLLM = 8;
  RCC_OscInitStruct.PLL.PLLN = 360;
  RCC_OscInitStruct.PLL.PLLP = RCC_PLLP_DIV2;
  RCC_OscInitStruct.PLL.PLLQ = 7;
  if (HAL_RCC_OscConfig(&RCC_OscInitStruct) != HAL_OK) { while (1) {} }

  if (HAL_PWREx_EnableOverDrive() != HAL_OK) { while (1) {} }

  RCC_ClkInitStruct.ClockType = (RCC_CLOCKTYPE_SYSCLK | RCC_CLOCKTYPE_HCLK |
                                 RCC_CLOCKTYPE_PCLK1  | RCC_CLOCKTYPE_PCLK2);
  RCC_ClkInitStruct.SYSCLKSource = RCC_SYSCLKSOURCE_PLLCLK;
  RCC_ClkInitStruct.AHBCLKDivider = RCC_SYSCLK_DIV1;
  RCC_ClkInitStruct.APB1CLKDivider = RCC_HCLK_DIV4;
  RCC_ClkInitStruct.APB2CLKDivider = RCC_HCLK_DIV2;
  if (HAL_RCC_ClockConfig(&RCC_ClkInitStruct, FLASH_LATENCY_5) != HAL_OK) { while (1) {} }
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
  uint32_t sr = USART3->SR;
  if (sr & USART_SR_RXNE) { uart_rx_push((uint8_t)(USART3->DR & 0xFF)); }
  else if (sr & (USART_SR_ORE | USART_SR_FE | USART_SR_NE))
  {
    uart_rx_lost++;
    (void)USART3->DR;            /* F4 clears these by reading SR then DR */
  }
}

void uart_write(const uint8_t *p, uint32_t n)
{
  /* Direct register poll: HAL_UART_Transmit's per-call overhead is large next
     to a 20-byte trace record. */
  while (n--)
  {
    while (!(USART3->SR & USART_SR_TXE)) { }
    USART3->DR = *p++;
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
