# NUCLEO-F429ZI. Cortex-M4 at 180 MHz, LAN8742 PHY, console on USART3.
#
# Note hal_flash_ramfunc, which the F7 does not build: the F4 HAL puts some
# flash routines in RAM and the link fails without it.
CUBE_FAMILY  = F4
HAL_REPO     = stm32f4xx-hal-driver
CMSIS_REPO   = cmsis-device-f4
HAL_DIR      = STM32F4xx_HAL_Driver
HAL_PREFIX   = stm32f4xx
CMSIS_DEV    = STM32F4xx
CMSIS_DEFINE = STM32F429xx
STARTUP      = startup_stm32f429xx.s
LDSCRIPT     = STM32F429ZITX_FLASH.ld
IT_SRC       = stm32f4xx_it.c
SYSTEM_SRC   = system_stm32f4xx.c
CPU          = -mcpu=cortex-m4 -mthumb -mfpu=fpv4-sp-d16 -mfloat-abi=hard

HAL_MODULES  = hal hal_cortex hal_rcc hal_rcc_ex hal_gpio hal_dma hal_pwr \
               hal_pwr_ex hal_flash hal_flash_ex hal_flash_ramfunc hal_eth \
               hal_uart

# 192 KB of record ring overflows this part's RAM by 46 848 bytes; 140 KB left
# headroom for lwIP's pools and the stack.  v2 adds a request bank, which is
# more .bss again, so the ring gives some back: the ring only bounds how many
# exchanges ONE batch holds, and the host chunks a capture across batches
# anyway, whereas the bank bounds how many DISTINCT requests a replay-protected
# victim can be given before the host has to stop and upload more.
BOARD_DEFS   = -DET_RING_BYTES='(96u*1024u)' -DET_BANK_BYTES='(32u*1024u)' -DET_BANK_MAX=1024u
