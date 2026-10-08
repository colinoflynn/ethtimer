# NUCLEO-F746ZG. Cortex-M7 at 216 MHz, LAN8742 PHY, console on USART3.
CUBE_FAMILY  = F7
HAL_REPO     = stm32f7xx-hal-driver
CMSIS_REPO   = cmsis-device-f7
HAL_DIR      = STM32F7xx_HAL_Driver
HAL_PREFIX   = stm32f7xx
CMSIS_DEV    = STM32F7xx
CMSIS_DEFINE = STM32F746xx
STARTUP      = startup_stm32f746xx.s
LDSCRIPT     = STM32F746ZGTX_FLASH.ld
IT_SRC       = stm32f7xx_it.c
SYSTEM_SRC   = system_stm32f7xx.c
CPU          = -mcpu=cortex-m7 -mthumb -mfpu=fpv5-sp-d16 -mfloat-abi=hard

HAL_MODULES  = hal hal_cortex hal_rcc hal_rcc_ex hal_gpio hal_dma hal_pwr \
               hal_pwr_ex hal_flash hal_flash_ex hal_eth hal_uart

# The ring was 192 KB before v2 added the request bank.  Both come out of the
# same .bss, and they bound different things: the ring is how many exchanges one
# batch holds (the host chunks across batches regardless), the bank is how many
# distinct requests a replay-protected victim can be given per upload.
BOARD_DEFS   = -DET_RING_BYTES='(128u*1024u)' -DET_BANK_BYTES='(64u*1024u)'
