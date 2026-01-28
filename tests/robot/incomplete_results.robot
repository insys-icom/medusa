*** Settings ***
Documentation       Suite for manually checking that HTML report/log output
...                 still works when one or more suites produced invalid XML
...                 output. This suite runs twice, once successfully and once
...                 unsuccessfully.
Metadata            medusa:for    ${DEP}    ${SLEEP_TIME}    IN    &{RUNS}
Metadata            medusa:deps    ${DEP}
Metadata            medusa:stage    1
Metadata            medusa:timeout    5,5,3


*** Variables ***
#    DEP=SLEEP_TIME
&{RUNS}
...                 working=2s
...                 broken=10s

${DEP}              ${None}
${SLEEP_TIME}       ${None}


*** Test Cases ***
Waste Time
    [Documentation]    This test gets interrupted by the soft timeout after
    ...    5 seconds and then skips to teardown. In one medusa:for case, the
    ...    suite terminates nicely before the hard timeout, in the other case
    ...    it runs into the hard timeout and should produce an invalid XML.
    ...    The merge then should still produce an incomplete log/report HTML.
    Sleep    20s
    [Teardown]    Sleep    ${SLEEP_TIME}
