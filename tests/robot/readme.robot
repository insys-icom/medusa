*** Settings ***
Documentation    Using `medusa:for`, this suite is executed three times in two
...    different stages and with different dependencies each time. The two
...    executions in stage 0 are run in parallel since their dependencies don't
...    overlap. One port is picked arbitrarily from a different list of ports
...    in each run.
...    The suite has a soft timeout of 300 seconds, a hard timeout of 30
...    seconds and a kill timeout of 5 seconds.
Metadata    medusa:for        $STAGE    $DUT1    $DUT2    $PORTS    IN    $RUNS
Metadata    medusa:deps       $DUT1    $DUT2   ANY $PORT IN $PORTS
Metadata    medusa:stage      $STAGE
Metadata    medusa:timeout    300,30,5


*** Variables ***
@{RUNS}      ${RUN1}    ${RUN2}    ${RUN3}

# Targets:   STAGE    DUT1     DUT2    PORTS
@{RUN1}      0        one      two     ${PORTS1}
@{RUN2}      0        three    four    ${PORTS2}
@{RUN3}      1        one      four    ${PORTS3}

@{PORTS1}    12      34      56
@{PORTS2}    123     456     789
@{PORTS3}    1234    5678    9012

# Set by medusa:for
${STAGE}     ${None}
${DUT1}      ${None}
${DUT2}      ${None}
${PORTS}     ${None}

# Set by medusa:deps (dynamic)
${PORT}     ${None}


*** Test Cases ***
Do Something
    Log    STAGE=${STAGE}, DUT1=${DUT1}, DUT2=${DUT2}, PORT=${PORT}
