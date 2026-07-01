*** Settings ***
Metadata    medusa:stage    0
Metadata    medusa:deps    a    b
Metadata    medusa:retry    2

Library    OperatingSystem


*** Test Cases ***
This will fail twice and pass on second retry
    TRY
        OperatingSystem.File Should Exist    /tmp/medusa_retry_test2_1
    EXCEPT    AS    ${error}
        OperatingSystem.Create File    /tmp/medusa_retry_test2_1
        Fail    ${error}
    END

    TRY
        OperatingSystem.File Should Exist    /tmp/medusa_retry_test2_2
    EXCEPT    AS    ${error}
        OperatingSystem.Create File    /tmp/medusa_retry_test2_2
        Fail    ${error}
    END

    OperatingSystem.Remove File    /tmp/medusa_retry_test2_1
    OperatingSystem.Remove File    /tmp/medusa_retry_test2_2


