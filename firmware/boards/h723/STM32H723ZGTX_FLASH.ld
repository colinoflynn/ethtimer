/*
*****************************************************************************
**  Linker script for the STM32H723ZG on a NUCLEO-H723ZG, running ethtimer.
**
**  Derived from the SW4STM32/CubeIDE template, with the memory split that an
**  H7 needs and the two Cortex-M4/M7 boards do not.
**
**  WHERE THINGS GO, AND WHY.
**
**    FLASH   1024K @ 0x08000000   .text, .rodata, the vector table
**    DTCM     128K @ 0x20000000   the stack, and nothing else
**    AXI      320K @ 0x24000000   .data and .bss -- which is where the record
**                                 ring and the request bank live, and they are
**                                 most of it
**    RAM_D2    32K @ 0x30000000   the Ethernet DMA descriptors
**    RAM_D3    16K @ 0x38000000   unused here; declared so the map says so
**
**  THE SPLIT IS NOT A STYLE CHOICE. The Ethernet DMA is a bus master in the D2
**  domain and **cannot reach DTCM at all** -- DTCM is private to the core. A
**  descriptor ring placed there produces no error: the MAC reads whatever the
**  bus returns, finds no descriptor it owns, and the interface is simply never
**  up. So the descriptors and the receive buffers are forced into RAM_D2 by
**  section attributes in ethernetif.c, and this script is where those sections
**  are given their addresses.
**
**  The stack is in DTCM because that is the fastest RAM the core has and
**  nothing else wants it. .bss cannot be: it is a quarter of a megabyte.
**
**  There is no cache carve-out here, because this build runs with the L1
**  D-cache OFF -- see board.c. That is what lets all of this be ordinary
**  memory rather than MPU regions with cache maintenance around every
**  descriptor and buffer touch.
*****************************************************************************
*/

ENTRY(Reset_Handler)

/* Top of DTCM. The stack grows down from here. */
_estack = 0x20020000;

_Min_Heap_Size = 0x200;
_Min_Stack_Size = 0x1000;

MEMORY
{
  FLASH  (rx)  : ORIGIN = 0x08000000, LENGTH = 1024K
  DTCM   (xrw) : ORIGIN = 0x20000000, LENGTH = 128K
  RAM_D1 (xrw) : ORIGIN = 0x24000000, LENGTH = 320K
  RAM_D2 (xrw) : ORIGIN = 0x30000000, LENGTH = 32K
  RAM_D3 (xrw) : ORIGIN = 0x38000000, LENGTH = 16K
}

SECTIONS
{
  .isr_vector :
  {
    . = ALIGN(4);
    KEEP(*(.isr_vector))
    . = ALIGN(4);
  } >FLASH

  .text :
  {
    . = ALIGN(4);
    *(.text)
    *(.text*)
    *(.glue_7)
    *(.glue_7t)
    *(.eh_frame)

    KEEP (*(.init))
    KEEP (*(.fini))

    . = ALIGN(4);
    _etext = .;
  } >FLASH

  .rodata :
  {
    . = ALIGN(4);
    *(.rodata)
    *(.rodata*)
    . = ALIGN(4);
  } >FLASH

  .ARM.extab   : { *(.ARM.extab* .gnu.linkonce.armextab.*) } >FLASH
  .ARM : {
    __exidx_start = .;
    *(.ARM.exidx*)
    __exidx_end = .;
  } >FLASH

  .preinit_array     :
  {
    PROVIDE_HIDDEN (__preinit_array_start = .);
    KEEP (*(.preinit_array*))
    PROVIDE_HIDDEN (__preinit_array_end = .);
  } >FLASH
  .init_array :
  {
    PROVIDE_HIDDEN (__init_array_start = .);
    KEEP (*(SORT(.init_array.*)))
    KEEP (*(.init_array*))
    PROVIDE_HIDDEN (__init_array_end = .);
  } >FLASH
  .fini_array :
  {
    PROVIDE_HIDDEN (__fini_array_start = .);
    KEEP (*(SORT(.fini_array.*)))
    KEEP (*(.fini_array*))
    PROVIDE_HIDDEN (__fini_array_end = .);
  } >FLASH

  /* used by the startup to initialize data */
  _sidata = LOADADDR(.data);

  .data :
  {
    . = ALIGN(4);
    _sdata = .;
    *(.data)
    *(.data*)
    . = ALIGN(4);
    _edata = .;
  } >RAM_D1 AT> FLASH

  . = ALIGN(4);
  .bss :
  {
    _sbss = .;
    __bss_start__ = _sbss;
    *(.bss)
    *(.bss*)
    *(COMMON)
    . = ALIGN(4);
    _ebss = .;
    __bss_end__ = _ebss;
  } >RAM_D1

  /* The heap check, and the stack it has to leave room for, are against DTCM
     rather than against .bss's region: the stack is in DTCM here. */
  ._user_heap_stack :
  {
    . = ALIGN(8);
    PROVIDE ( end = . );
    PROVIDE ( _end = . );
    . = . + _Min_Heap_Size;
    . = ALIGN(8);
  } >RAM_D1

  ._stack_room (NOLOAD) :
  {
    . = ALIGN(8);
    . = . + _Min_Stack_Size;
    . = ALIGN(8);
  } >DTCM

  /* ---------------------------------------------------------------------- */
  /* Ethernet DMA descriptors, in D2 where the MAC's own bus master lives.
   *
   * NOLOAD: these are uninitialised and must not be given a load image --
   * startup copies .data from flash and zeroes .bss, and it knows about
   * neither. The driver writes the rings before the interface comes up.
   *
   * THE RECEIVE BUFFERS ARE NOT HERE. They are lwIP's RX_POOL, which comes out
   * of .bss in AXI SRAM: the pool is about 16 KB and lwIP's heap and pbuf pool
   * another 28 KB, so moving them here would not fit in 32 KB, and moving only
   * the one pool is not expressible -- lwIP places every pool through a single
   * LWIP_DECLARE_MEMORY_ALIGNED macro. The Ethernet DMA reaches AXI SRAM
   * through the D2-to-D1 bridge, so this works; it costs the receive path a
   * domain crossing per buffer, which an instrument that spends its time
   * waiting on a wire can afford. It is also the one placement decision in
   * this board that a working Ethernet bring-up would confirm and that has not
   * been confirmed -- see README.md. */
  .eth_desc (NOLOAD) :
  {
    . = ABSOLUTE(0x30000000);
    *(.RxDescripSection)
    . = ALIGN(32);
    *(.TxDescripSection)
    . = ALIGN(32);
  } >RAM_D2

  /* Descriptors that do not fit link silently and are placed past the end of
     RAM_D2, at an address with nothing behind it; the symptom is an interface
     that never receives a frame. */
  ASSERT(SIZEOF(.eth_desc) <= LENGTH(RAM_D2),
         "Ethernet descriptors do not fit in RAM_D2 (32K)")

  /DISCARD/ :
  {
    libc.a ( * )
    libm.a ( * )
    libgcc.a ( * )
  }

  .ARM.attributes 0 : { *(.ARM.attributes) }
}
