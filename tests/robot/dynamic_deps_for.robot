*** Settings ***
Metadata    medusa:for    ${DUT}    IN      @{DUTS}
Metadata    medusa:stage    0
Metadata    medusa:deps     ${DUT}


*** Variables ***
${DUT}    ${None}
@{DUTS}
...    one
...    two
...    three
...    ANY $DUT IN $DUT_OPTIONS
@{DUT_OPTIONS}    a    b    c


*** Test Cases ***
Do Nothing
    Log    MEDUSA_DYNAMIC=${MEDUSA_DYNAMIC}, MEDUSA_DEPS=${MEDUSA_DEPS}, MEDUSA_FOR=${MEDUSA_FOR}
