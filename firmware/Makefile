# ethtimer -- Ethernet request/response timing instrument.
#
#   ../tools/fetch_sdk.sh f429    # once: pull the pinned ST sources
#   make BOARD=f429               # -> Build/f429/ethtimer.bin
#   make BOARD=f429 APP=victim    # -> Build/f429-victim/ethtimer_victim.bin
#   make                          # -> Build/f746/ethtimer.bin  (default board)
#   make list                     # the boards and applications this tree knows
#   make SDK=cube CUBE=/path/to/STM32CubeF4 BOARD=f429
#
# Everything under Build/ and vendor/ is generated or fetched; neither is in git.
#
# TWO SDK LAYOUTS, ONE SET OF VARIABLES.
#
#   SDK=vendor (the default) reads vendor/, which `tools/fetch_sdk.sh` fills
#   with ST's per-component repositories at pinned tags. This is what CI uses
#   and what a fresh clone should use.
#
#   SDK=cube reads a monolithic STM32CubeXX package at $(CUBE). Useful if you
#   already have one installed, and the reason the paths below are split into
#   HAL_INC/HAL_SRC/... rather than derived from one root: each component
#   repository's root IS the subtree the Cube package puts it under, so the two
#   layouts differ only in these five assignments.
#
# ONE BUILD TREE PER BOARD. Objects go to Build/$(BOARD)/, so building for one
# board cannot silently link the other's objects -- the two differ in CPU flags,
# and an object linked from the wrong tree is a hard fault at reset rather than
# a link error.

BOARD ?= f746
SDK   ?= vendor

BDIR   = boards/$(BOARD)
-include $(BDIR)/board.mk

# A BOARD IS A DIRECTORY WITH A board.mk IN IT, not just a directory under
# boards/: that also holds a README.md, and counting it as a board is how
# `make list` first came to try reading boards/README.md/board.mk.
BOARDS := $(patsubst boards/%/board.mk,%,$(wildcard boards/*/board.mk))

ifeq ($(CUBE_FAMILY),)
$(error unknown BOARD "$(BOARD)" -- expected boards/$(BOARD)/board.mk. Known: $(BOARDS))
endif

VENDOR = vendor

ifeq ($(SDK),vendor)
  HAL_INC   = $(VENDOR)/$(HAL_REPO)/Inc
  HAL_SRC   = $(VENDOR)/$(HAL_REPO)/Src
  CORE_INC  = $(VENDOR)/cmsis-core/Include
  DEV_INC   = $(VENDOR)/$(CMSIS_REPO)/Include
  LWIP      = $(VENDOR)/stm32-mw-lwip
  LAN       = $(VENDOR)/stm32-lan8742
  SDK_CHECK = $(HAL_INC)/$(HAL_PREFIX)_hal.h
else ifeq ($(SDK),cube)
  CUBE     ?= /mnt/c/dev/sdk/STM32Cube$(CUBE_FAMILY)
  HAL_INC   = $(CUBE)/Drivers/$(HAL_DIR)/Inc
  HAL_SRC   = $(CUBE)/Drivers/$(HAL_DIR)/Src
  CORE_INC  = $(CUBE)/Drivers/CMSIS/Include
  DEV_INC   = $(CUBE)/Drivers/CMSIS/Device/ST/$(CMSIS_DEV)/Include
  LWIP      = $(CUBE)/Middlewares/Third_Party/LwIP
  LAN       = $(CUBE)/Drivers/BSP/Components/lan8742
  SDK_CHECK = $(HAL_INC)/$(HAL_PREFIX)_hal.h
else
  $(error SDK must be "vendor" or "cube", got "$(SDK)")
endif

CROSS  = arm-none-eabi-
CC     = $(CROSS)gcc
AS     = $(CROSS)gcc -x assembler-with-cpp
CP     = $(CROSS)objcopy
SZ     = $(CROSS)size

# THREE APPLICATIONS, ONE BOARD SUPPORT.
#
#   APP=instrument (default)  the thing that takes the measurement
#   APP=victim                the reference responder, on lwIP, every board
#   APP=victim-raw            the same responder with no TCP/IP stack, f429 only
#
# All three share boards/<b>/ -- same clock tree, console and interrupt file --
# and differ in which application directory is compiled. That is deliberate: a
# responder built on its own board support would be a second thing that could be
# wrong, and the first question about any jitter number is which end of the cable
# it came from.
#
# victim-raw additionally omits lwIP and the lwIP port. The lwIP half of board.c
# is still compiled and is dropped by --gc-sections, because nothing in that
# application references it; what it does use is board_clock_console_init(),
# which exists so this responder shares the instrument's clock tree rather than
# carrying a copy of it.
#
# WHY victim-raw IS ONE BOARD. It drives the Ethernet DMA descriptors and the
# DMASR/DMATPDR registers directly, and the H7's DMA is a different generation
# with a different descriptor format and a per-channel status register. A
# board-generic version of it would be a board-generic version of the thing it
# exists to avoid. APP=victim is the portable responder.
#
# Separate build trees, for the same reason the boards have separate ones: the
# applications' objects share basenames (main.o), and one tree would link one
# application's main with another's body.
APP ?= instrument

ifeq ($(APP),instrument)
  TARGET  = ethtimer
  BUILD   = Build/$(BOARD)
  APP_SRC = src/main.c src/ethtimer.c
  APP_INC = -Iinc
  APP_LWIP = yes
else ifeq ($(APP),victim)
  TARGET  = ethtimer_victim
  BUILD   = Build/$(BOARD)-victim
  APP_SRC = victim/src/main.c victim/src/victim.c common/pwcheck.c
  APP_INC = -Ivictim/inc -Icommon
  APP_LWIP = yes
else ifeq ($(APP),victim-raw)
  TARGET  = ethtimer_victim_raw
  BUILD   = Build/$(BOARD)-victim-raw
  APP_SRC = victim_raw/src/main.c victim_raw/src/rawnet.c common/pwcheck.c
  # The WIRE format is victim/inc's, shared unchanged, so that one host adapter
  # measures either responder and the two are comparable. common/ holds the
  # password check, compiled into both responders for the same reason: the
  # thing being measured has to be the same code on both.
  APP_INC = -Ivictim_raw/inc -Ivictim/inc -Icommon
  APP_LWIP = no
  ifneq ($(BOARD),f429)
    $(error APP=victim-raw is f429 only -- it drives the F4 Ethernet DMA \
descriptors directly. Use APP=victim for other boards.)
  endif
else
  $(error APP must be "instrument", "victim" or "victim-raw", got "$(APP)")
endif

# ---------------------------------------------------------------- sources ---
C_SRC = \
  $(APP_SRC) \
  $(BDIR)/board.c \
  $(BDIR)/$(IT_SRC) \
  $(BDIR)/$(SYSTEM_SRC) \
  $(LAN)/lan8742.c

C_SRC += $(foreach m,$(HAL_MODULES),$(HAL_SRC)/$(HAL_PREFIX)_$(m).c)

ifeq ($(APP_LWIP),yes)
  # The lwIP port, and lwIP: core + IPv4 + netif, the CubeMX NO_SYS selection.
  C_SRC += $(BDIR)/ethernetif.c
  C_SRC += $(wildcard $(LWIP)/src/core/*.c) \
           $(wildcard $(LWIP)/src/core/ipv4/*.c) \
           $(LWIP)/src/netif/ethernet.c
endif

ASM_SRC = $(BDIR)/$(STARTUP)

# --------------------------------------------------------------- includes ---
# lwIP's headers are on the path even for an application that does not LINK it:
# board.c includes them, and its lwIP-using functions are compiled and then
# dropped by --gc-sections rather than being #ifdef'd out of existence. One
# board.c with one clock tree, not two that can drift apart.
INC = $(APP_INC) -I$(BDIR) \
      -I$(CORE_INC) -I$(DEV_INC) \
      -I$(HAL_INC) -I$(HAL_INC)/Legacy \
      -I$(LWIP)/src/include -I$(LWIP)/system -I$(LWIP)/system/arch \
      -I$(LAN)

# ------------------------------------------------------------------ flags ---
DEFS = -D$(CMSIS_DEFINE) -DUSE_HAL_DRIVER $(BOARD_DEFS) $(EXTRA_DEFS)

OPT   = -O2
WARN  = -Wall -Wextra -Wno-unused-parameter
# -MMD -MP: emit a .d per object listing the headers it included, so that
# editing a HEADER rebuilds what depends on it.  Without this the objects
# depended on the .c and the Makefile only, and changing inc/ethtimer.h -- the
# one file that defines the wire protocol -- produced no rebuild at all.  The
# binary then kept answering with the old limits while the header and the host's
# mirror of it both said otherwise, which reads as a device disagreeing with its
# own protocol rather than as a stale build.
CFLAGS  = $(CPU) $(DEFS) $(INC) $(OPT) $(WARN) -fdata-sections -ffunction-sections \
          -MMD -MP
ASFLAGS = $(CPU) $(OPT)
LDFLAGS = $(CPU) -T$(BDIR)/$(LDSCRIPT) -specs=nano.specs -specs=nosys.specs \
          -Wl,-Map=$(BUILD)/$(TARGET).map,--cref -Wl,--gc-sections -lc -lm

OBJ  = $(addprefix $(BUILD)/,$(notdir $(C_SRC:.c=.o)))
vpath %.c $(sort $(dir $(C_SRC)))
OBJ += $(addprefix $(BUILD)/,$(notdir $(ASM_SRC:.s=.o)))
vpath %.s $(sort $(dir $(ASM_SRC)))

all: $(BUILD)/$(TARGET).bin

# NAME THE MISSING SDK, rather than letting the compiler report a missing
# header. A clone that has not run tools/fetch_sdk.sh fails on the first HAL
# include, hundreds of lines in, and reads as a broken repository.
$(SDK_CHECK):
ifeq ($(SDK),vendor)
	@echo "the pinned ST sources are not in firmware/vendor/." >&2
	@echo "Run:  tools/fetch_sdk.sh $(BOARD)" >&2
	@false
else
	@echo "no HAL for $(BOARD) under CUBE=$(CUBE)" >&2
	@echo "Expected $(SDK_CHECK). Point CUBE= at an STM32Cube$(CUBE_FAMILY) package," >&2
	@echo "or drop SDK=cube and run:  tools/fetch_sdk.sh $(BOARD)" >&2
	@false
endif

$(BUILD)/%.o: %.c Makefile $(SDK_CHECK) | $(BUILD)
	$(CC) -c $(CFLAGS) -Wa,-a,-ad,-alms=$(BUILD)/$(notdir $(<:.c=.lst)) $< -o $@

$(BUILD)/%.o: %.s Makefile | $(BUILD)
	$(AS) -c $(ASFLAGS) $< -o $@

$(BUILD)/$(TARGET).elf: $(OBJ) Makefile
	$(CC) $(OBJ) $(LDFLAGS) -o $@
	$(SZ) $@

$(BUILD)/$(TARGET).bin: $(BUILD)/$(TARGET).elf
	$(CP) -O binary -S $< $@

$(BUILD):
	mkdir -p $@

list:
	@for b in $(BOARDS); do \
	    printf "  %-6s %s\n" "$$b" \
	      "$$(sed -n '1s/^# *//p' boards/$$b/board.mk)"; \
	done
	@echo
	@echo "  APP=instrument (default)  the measuring end"
	@echo "  APP=victim                the reference responder"

# `clean` removes THIS board-and-application's tree only.  It used to remove
# Build/ entirely, which meant cleaning one board silently discarded the other
# five binaries -- including ones already flashed to a board and being measured,
# whose .elf is the only way to map an address afterwards.
clean:
	rm -rf $(BUILD)

clean-all:
	rm -rf Build

# The generated header dependencies. `-` because they do not exist on a clean
# tree, and their absence is not an error -- the .o does not exist either.
-include $(OBJ:.o=.d)

.PHONY: all clean clean-all list
