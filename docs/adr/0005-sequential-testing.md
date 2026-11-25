# ADR-0005: Sequential testing (confidence sequences) for experiments

**Status:** accepted

## Context
Teams peek at experiments daily. Repeated fixed-horizon tests inflate false positives (bench/aa_simulation.py: about 23% at α=0.05 with 10 looks).

## Decision
Decisions and auto-stop use asymptotic confidence sequences (Waudby-Smith et al. 2023, as in GrowthBook). Fixed-horizon Welch results are reported for reference. An SRM check gates every readout. CUPED is optional.

## Consequences
+ Always-valid peeking. − Wider intervals, so detecting a given effect needs more samples. Tunable via n_star.
