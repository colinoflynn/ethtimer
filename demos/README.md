# Demos

Each directory is a **protocol adapter** — a subclass of
[`ethtimer.target.Target`](../ethtimer/target.py) — plus whatever protocol code
it needs to build a request and read a reply.

They are demos in both senses: they take real measurements, and they are the
worked examples to copy when pointing the instrument at something else.

| demo | transport | mode | needs at the other end | verifiable |
|---|---|---|---|---|
| [`dns`](dns) | UDP 53 | `bank` | **any resolver** | no — there is no key |
| [`snmpv3`](snmpv3) | UDP 161 | `fixed` | an SNMPv3 authPriv agent, credentials known | yes |
| [`oscore`](oscore) | UDP 5683 | `bank` | an OSCORE server, master secret known | yes |
| [`tls`](tls) | TCP 443 | `relay` then `bank` | an HTTPS server, TLS 1.2 CBC, key log available | yes |

**Start with [`dns`](dns).** It is the only one whose other end you already
have, so it is the demo that separates "my build is wrong" from "my adapter is
wrong" — and those two failures look identical from a capture that returns
nothing.

## The import edge runs one way

Nothing under [`../ethtimer/`](../ethtimer) imports anything from here, and
nothing here is packaged. An adapter is found by path: `--target <name>` looks
it up in a table in [`cli.py`](../ethtimer/cli.py), and `--module <file>` loads
any file at all.

That is deliberate. The instrument has to stay importable with nothing but
`numpy` and `pyserial` on a machine that has a board attached and no interest in
SNMP; and an adapter has to be able to depend on whatever it likes without that
becoming the instrument's dependency. The TLS demo's certificate helper wants
`cryptography`; the instrument does not, and never will because of it.

## What every adapter owes the capture

Three things, and the third is the one that is easy to get wrong:

1. **A request**, and for `bank` mode a *fresh* one per exchange.
2. **A window** — where in the reply the interesting bytes are. Taken from a
   real reply rather than a constant, because a constant was wrong the day a
   length field in the reply grew by a byte.
3. **A pair**, `aes_in` and `aes_out`, formed from what is on the wire and
   **without the key**. That pair is what lets `campaign` state
   `E(K, aes_in) == aes_out` at the end and raise if it does not hold. An
   adapter that cannot produce one says so by returning `None` from
   `truth_key()`, and its captures are reported as **NOT CHECKED** rather than
   as passing.

See [`../ethtimer/target.py`](../ethtimer/target.py) for every hook and the
measured failure each one exists to prevent.

## Adding one

```bash
cp demos/dns/dns_target.py demos/mything/mything_target.py
# edit: name, port, mode, prepare(), requests(), window(), blocks()
python -m ethtimer.cli --module demos/mything/mything_target.py --n 2000
```

When it earns a name, add a line to `_BUILTIN` in
[`../ethtimer/cli.py`](../ethtimer/cli.py) and a README here saying what the
other end has to be. That table is written out rather than scanned so that
`--list` is honest about what exists without importing every demo, and so a
typo names the file it could not find.
