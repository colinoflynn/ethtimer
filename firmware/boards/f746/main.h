/**
  ******************************************************************************
  * @file    main.h
  * @brief   HAL and netif configuration for the NUCLEO-F746ZG board support.
  ******************************************************************************
  * Derived from STM32CubeF7
  * Projects/STM32F746ZG-Nucleo/Applications/LwIP/LwIP_HTTP_Server_Netconn_RTOS.
  * Copyright (c) 2016 STMicroelectronics.
  ******************************************************************************
  */
#ifndef __MAIN_H
#define __MAIN_H

#include "stm32f7xx_hal.h"

/* The addresses the interface comes up with.  They are only a starting
 * point: SET_NET re-addresses the interface at run time and the host always
 * sends it, so one binary drives a device on any subnet.
 *
 * They are 192.168.100.x rather than the other board's 192.168.7.x for no
 * better reason than that a device measured with this board defaulted to
 * that subnet when it found no DHCP server.  Nothing depends on either. */
#define IP_ADDR0   ((uint8_t)192U)
#define IP_ADDR1   ((uint8_t)168U)
#define IP_ADDR2   ((uint8_t)100U)
#define IP_ADDR3   ((uint8_t)20U)

#define NETMASK_ADDR0   ((uint8_t)255U)
#define NETMASK_ADDR1   ((uint8_t)255U)
#define NETMASK_ADDR2   ((uint8_t)255U)
#define NETMASK_ADDR3   ((uint8_t)0U)

#define GW_ADDR0   ((uint8_t)192U)
#define GW_ADDR1   ((uint8_t)168U)
#define GW_ADDR2   ((uint8_t)100U)
#define GW_ADDR3   ((uint8_t)1U)

#define VICTIM_IP0 ((uint8_t)192U)
#define VICTIM_IP1 ((uint8_t)168U)
#define VICTIM_IP2 ((uint8_t)100U)
#define VICTIM_IP3 ((uint8_t)10U)

/* MAC comes from stm32f7xx_hal_conf.h; override the last byte so the two
   Nucleos do not share ST's default address on the same link. */
#undef  ETH_MAC_ADDR5
#define ETH_MAC_ADDR5   ((uint8_t)0x20)

#ifndef CONSOLE_BAUD
#define CONSOLE_BAUD 921600U
#endif

void uart_write(const uint8_t *p, uint32_t n);
void uart_puts(const char *s);
void uart_puthex32(uint32_t v);
void uart_putdec(uint32_t v);
int  uart_getchar_nb(void);

#endif /* __MAIN_H */
