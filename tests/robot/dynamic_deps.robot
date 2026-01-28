*** Settings ***
Metadata    medusa:for    $CURRENT_RUN    IN      $RUNS
Metadata    medusa:stage    0
Metadata    medusa:deps     ANY $DYN1 IN $SRC1    ANY $DYN2 IN $SRC2


*** Variables ***
${CURRENT_RUN}    ${None}
${DYN1}           ${None}
${DYN2}           ${None}
@{SRC1}           1.1    1.2    any.1    any.2
@{SRC2}           2.1    2.2    any.1    any.2
@{RUNS}           1      2      3


*** Test Cases ***
Do Nothing
    Log    DYN1=${DYN1} DYN2=${DYN2} MEDUSA_DYNAMIC=${MEDUSA_DYNAMIC}
