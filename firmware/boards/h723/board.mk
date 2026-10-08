# NUCLEO-H723ZG. Cortex-M7 at 400 MHz, LAN8742 PHY, console on USART3.
#
# UNVERIFIED ON ETHERNET -- see README.md in this directory. The board boots,
# clocks, consoles and answers the control protocol; no capture has been taken
# with it, because the board this was written on has no cable in its RJ45.
#
# Note hal_eth_ex, which the F4 and F7 builds do not need: the H7's ETH HAL
# splits the extended entry points into their own translation unit and
# ethernetif.c's MAC configuration reaches them.
CUBE_FAMILY  = H7
HAL_REPO     = stm32h7xx-hal-driver
CMSIS_REPO   = cmsis-device-h7
HAL_DIR      = STM32H7xx_HAL_Driver
HAL_PREFIX   = stm32h7xx
CMSIS_DEV    = STM32H7xx
CMSIS_DEFINE = STM32H723xx
STARTUP      = startup_stm32h723xx.s
LDSCRIPT     = STM32H723ZGTX_FLASH.ld
IT_SRC       = stm32h7xx_it.c
SYSTEM_SRC   = system_stm32h7xx.c
CPU          = -mcpu=cortex-m7 -mthumb -mfpu=fpv5-d16 -mfloat-abi=hard

HAL_MODULES  = hal hal_cortex hal_rcc hal_rcc_ex hal_gpio hal_dma hal_dma_ex \
               hal_pwr hal_pwr_ex hal_flash hal_flash_ex hal_eth hal_eth_ex \
               hal_uart hal_uart_ex hal_exti hal_mdma

# 320 KB of AXI SRAM holds .data and .bss, and the ring plus the bank is most
# of it: 128 + 64 KB leaves about 128 KB for lwIP's pools, the stack guard and
# everything else, which is the same split the 320 KB Cortex-M7 board uses.
# The Ethernet descriptors and the Rx pool are NOT in here -- they are forced
# into the 32 KB D2 SRAM by the linker script, because the Ethernet DMA cannot
# reach DTCM and should not have to cross a domain for every descriptor.
BOARD_DEFS   = -DET_RING_BYTES='(128u*1024u)' -DET_BANK_BYTES='(64u*1024u)'
