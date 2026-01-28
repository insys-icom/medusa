*** Settings ***
Documentation       Medusa should insert variables corresponding to the resolved
...                 values of its suite metadata named MEDUSA_STAGE, MEDUSA_DEPS and
...                 MEDUSA_FOR.
Metadata            medusa:deps    plain    ${SCALAR_STRING}    ${SCALAR_NUMBER}    @{LIST}
Metadata            medusa:stage    my${STAGE}
Metadata            medusa:for    ${TARGET1}    ${TARGET2}    ${TARGET3}    IN    @{LIST_OF_LISTS}

Library             String
Library             Collections


*** Variables ***
${MEDUSA_STAGE}     ${None}    # Set by medusa:stage
${MEDUSA_DEPS}      ${None}    # Set by medusa:deps
${MEDUSA_FOR}       ${None}    # Set by medusa:for
${TARGET1}          ${None}    # Set by medusa:for
${TARGET2}          ${None}    # Set by medusa:for
${TARGET3}          ${None}    # Set by medusa:for

${SCALAR_STRING}    hello
${SCALAR_NUMBER}    ${42}
${SECOND}           two
@{LIST}             one    ${SECOND}    ${3}
${STAGE}            Special_Stage

@{SOURCE1}          one    ${SECOND}    three
@{SOURCE2}          a    b    c
@{SOURCE3}          ${1}    ${2}    ${3}
@{LIST_OF_LISTS}
...                 ${SOURCE1}
...                 ${SOURCE2}
...                 ${SOURCE3}

@{EXPECTED_DEPS}
...                 plain
...                 hello
...                 42
...                 one
...                 two
...                 3


*** Test Cases ***
Stage Variable Should Be Correct
    [Documentation]    Medusa should set the MEDUSA_STAGE variable to the value
    ...    of the medusa:stage metadata with all variables resolved as strings.
    Should Be Equal    ${MEDUSA_STAGE}    mySpecial_Stage
    ...    MEDUSA_STAGE value is not as expected!

Stage Metadata Should Be Correct
    [Documentation]    Medusa should set the medusa:stage metadata to the resolved result
    ...    of the user-specified medusa:stage metadata with all variables resolved as strings.
    Should Be Equal    ${MEDUSA_STAGE}    mySpecial_Stage
    ...    MEDUSA_STAGE value is not as expected!

Deps Variable Should Be Correct
    [Documentation]    Medusa should set the MEDUSA_DEPS variable to a list of
    ...    strings that contains all values in medusa:deps metadata. Lists
    ...    should be flattened to just their values and variables should be
    ...    resolved.
    # Convert set to list because set is not sortable, which is required for ignore_order compare
    Collections.Lists Should Be Equal    ${MEDUSA_DEPS}    ${EXPECTED_DEPS}    ignore_order=${True}

Deps Metadata Should Be Correct
    [Documentation]    Medusa should set the medusa:deps metadata to a resolved
    ...    and expanded string of values separated by four spaces.
    ${Deps}    Evaluate    re.split(r'${SPACE * 2}+', $suite_metadata["medusa:deps"])    modules=re
    Collections.Lists Should Be Equal    ${Deps}    ${EXPECTED_DEPS}    ignore_order=${True}

For Variable Should Be Correct
    [Documentation]    Medusa should set the MEDUSA_FOR variable to a dict
    ...    containing all medusa:for variable names and their values.
    VAR    @{Variable Names}    TARGET1    TARGET2    TARGET3
    VAR    @{Option1}    one    two    three
    VAR    @{Option2}    a    b    c
    VAR    @{Option3}    ${1}    ${2}    ${3}
    FOR    ${Option}    IN    ${Option1}    ${Option2}    ${Option3}
        TRY
            FOR    ${Key}    ${Value}    IN ZIP    ${Variable Names}    ${Option}
                Should Be Equal    ${MEDUSA_FOR}[${Key}]    ${Value}
            END

            Pass Execution    Found matching option '${Option}'
        EXCEPT
            No Operation
        END
    END

    Log    ${MEDUSA_FOR}
    Fail    MEDUSA_FOR variable was not set correctly!
