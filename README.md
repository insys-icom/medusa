# Medusa

___Medusa is a tool to easily parallelize execution of [Robot Framework](https://robotframework.org/) test suites.___

If you have a lot of Robot Framework tests that take a non-negligible amount of time, you can benefit greatly from running them in parallel. Medusa uses suite metadata to start suites in parallel dynamically while preventing resource usage conflicts. Suites can be assigned to sequentially executed stages and can be run multiple times with different variables, even in parallel.

This could be used to execute one or multiple suites against multiple target devices or endpoints simultaneously, or even to execute a single suite sequentially multiple times with different configuration.

Parallelization happens on suite level, it is not (currently) possible to run tests within a single suite in parallel. It is also not (currently) possible to have one suite directly depend on another suite to finish first, though you could use separate stages to achieve this. Consider using [pabot](https://pabot.org/) if you need these features.

Medusa is developed by [INSYS icom GmbH](https://insys-icom.com) and licensed as Open Source Software, see the [License](#license) section below for details.


# Table of Contents

1. [Installation](#installation)
    1. [User](#user)
    1. [Developer](#developer)
1. [Quick Start](#quick-start)
1. [Command line usage](#command-line-usage)
1. [Suite metadata](#suite-metadata)
1. [Contributing](#contributing)
    1. [Reporting bugs](#reporting-bugs)
    1. [Adding features](#adding-features)
1. [License](#license)


# Installation
Make sure you have installed python3 `pip` and `venv`.

## User
``` sh
python3 -m venv .venv     # Create a venv in .venv (if not done already)
. .venv/bin/activate      # Enter the venv
python3 -m pip install .  # Install medusa

# use medusa...

deactivate                # Exit the venv (when you are done)
```

## Developer
``` sh
python3 -m venv .venv  # Create a venv in .venv (if not done already)
. .venv/bin/activate   # Enter the venv
python3 -m pip install -e '.[dev]'  # Install editable with dev dependencies

# use medusa, do your developing...

make format            # Run formatter
make check             # Run type checker and linter
make fix               # Auto-fix linter suggestions (if possible)
make test              # Run tests
deactivate             # Exit the venv (when you are done)
```


# Quick Start
Add at least the required metadata `medusa:stage` and `medusa:deps` to your suite(s). Optionally add `medusa:timeout` for suite-specific timeouts or `medusa:for` for multiplying suites with different variables.

``` robot
*** Settings ***
Documentation    Stage 1, multiply suite with three different dependencies and
...              input values, timeout 5min (soft), 30s (hard), 5s (kill)
...              First suite:  $DEP=foo    $INPUT=input1
...              Second suite: $DEP=bar    $INPUT=input2
...              Third suite:  $DEP=baz    $INPUT=input3
Metadata    medusa:stage      1
Metadata    medusa:deps       ${DEP}
Metadata    medusa:for        ${DEP}    ${INPUT}    IN    &{DEP_INPUT_DICT}
Metadata    medusa:timeout    300,30,5

*** Variables ***
${DEP}               ${None}  # Set by medusa:for
${INPUT}             ${None}  # Set by medusa:for
&{DEP_INPUT_DICT}    foo=input1    bar=input2    baz=input3
```

Usage summary:
* `medusa stats`: View information about your suite(s)
* `medusa run`: Run the suite(s)
* Use robot options with `--` as a separator after medusa options: `medusa run -- -i security my_suite.robot`

Results are stored in the `results/` directory in a date/timestamped subdirectory by default, this can be changed with the `-d` option. See `medusa -h` and the detailed documentation below for more information.


# Command line usage
Run `medusa --help` for full usage information, this section is just a rough overview. Medusa supports two main actions, `stats` and `run`. You can use `help` to get additional info about some options and `version` to output medusa's version.

The `stats` and `run` commands accept arguments just like `robot`. Example with just suites as arguments:
``` sh
medusa stats single_suite.robot my_suite_dir/
```

You can also use (almost) all options that `robot` accepts. You need to write `--` before any `robot` options in order to separate them from Medusa's own options. In this example, we use `robot`'s `--dryrun` option:
``` sh
#                     Separator ──┲┓
medusa run --outputdir customdir/ -- --dryrun my_suite_dir/
#          ┗━━ Medusa Option ━━━┛    ┗━━━ Robot Option ━━━┛
```

## `medusa stats ...`
Medusa reads the specified suite(s) and outputs information about them. By default, only a short summary of stats is given but more information can be shown with the `-s` or `--select` option. This includes infomation about recognised stages, dependencies and suites, as well as tags. Additional options:
* Filter suites by stage/dependency with `-f` or `--filter`
* Output additional information with `-s` or `--select`

## `medusa run ...`
Medusa reads the specified suite(s) and executes them. Stages are sorted alphabetically and executed sequentially. Within each stage, medusa dynamically starts as many suite in parallel as possible without starting suites with overlapping dependencies. Additional options:
* Change the output directory with `-d` or `--outputdir`
* Filter suites by stage/dependency with `-f` or `--filter`
* Set global soft/hard/kill timeouts with `-t` or `--timeout` (can be overriden with suite metadata)

# Suite metadata
The order and parallelisation of suites is determined entirely by suite metadata. For this reason, every suite needs to have at least `medusa:stage` and `medusa:deps` metadata configured. The `medusa:for` and `medusa:timeout` metadata is optional.

The below examples use the `$VAR` escaped variable syntax but the regular `${VAR}` syntax works too.

## `medusa:stage` (required)
Each suite needs to be assigned to a stage using the `medusa:stage` metadata key. Stages are executed sequentially in alphanumeric order, meaning that two suites in different stages will never run in parallel. After all suites in one stage finished executing, the next stage is executed.

Example:
```robot
*** Settings ***
Metadata    medusa:stage    1_Example
```

Given these three suites:
* _Suite1_ with `medusa:stage` = _1_Example_
* _Suite2_ with `medusa:stage` = _1_Example_
* _Suite3_ with `medusa:stage` = _2_Potato_

The suites _Suite1_ and _Suite2_ will be executed first, possibly in parallel (if their dependencies allow it). Once both of them finished, _Suite3_ will be executed.


## `medusa:deps` (required)
Each suite needs to declare dependencies using the `medusa:deps` metadata key. If two suites in the same stage have overlapping dependencies, they are not executed in parallel.

Example:
```robot
*** Settings ***
Metadata    medusa:deps    foo    ${BAR}    @{BAZ}

*** Variables ***
${BAR}    bar
@{BAZ}    baz    buzz    butz
```

As you can see, `medusa:deps` takes a list of values separated by two or more spaces. You can either directly write the value or use scalar or list variables. In the case of list variables, the list is simply flattened. This means that the above example is equivalent to:
```robot
*** Settings ***
Metadata    medusa:deps    foo    bar    baz    buzz    butz
```

Given these three suites in the same stage:
* _Suite1_ with `medusa:deps` = _foo_, _bar_
* _Suite2_ with `medusa:deps` = _bar_, _baz_
* _Suite3_ with `medusa:deps` = _buzz_, _butz_

The suites _Suite1_ and _Suite2_ will not be executed in parallel because they both have the dependency _bar_. _Suite3_ will be executed in parallel to the other two because it does not have any dependencies in common with them.

### Dynamic dependencies
Medusa can also pick a dependency from a list of options at runtime, depending on whether one of the options is available. This can be done with the `ANY $ITEM IN $LIST` syntax. `$LIST` has to be a list variable and `$ITEM` needs to be defined with value `None` (`${None}` in Robot Framework) Here is an example for a suite that can run on any one device out of a list:
```robot
*** Settings ***
Metadata    medusa:deps    ANY $DUT IN $DEVICES

*** Variables
@{DEVICES}    dut1    dut2    dut3  # List of options
${DUT}    ${None}                   # Determined at runtime by medusa
```

When this suite is executed with medusa, `${DUT}` could have any one of the three values `dut1`, `dut2` or `dut3` depending on which ones is available (not currently used by another suite).

Dynamic dependencies can also be combined with `medusa:for`, for example in order to run the same suite on two devices from different lists of options.


## `medusa:for` (optional)
The `medusa:for` metadata key can be used to execute one suite multiple times with differently set suite variables. This could be used to run the same test suite in parallel against multiple endpoints or test devices or with slightly differing configuration. `medusa:for` expects the format `$TARGET    [$TARGET...]    IN    $SOURCE`. The `$SOURCE` is either a list or dictionary of input values and the `$TARGET`s are variable names to assign the input values to. The target variables have to be declared with value `${None}`.

Example with one target variable and simple input list:
```robot
*** Settings ***
Metadata    medusa:for    $DUT    IN    $DUTS

*** Variables ***
@{DUTS}    foo    bar  # Source (input values)
${DUT}    ${None}      # Target variable
```
The above example suite will be executed twice, once with `${DUT}` = _foo_ and once with `${DUT}` = _bar_.

Example with three target variables and two-dimensional input list:
```robot
*** Settings ***
Metadata    medusa:for   $FIRST    $SECOND    $THIRD    IN    $RUNS

*** Variables ***
@{RUNS}      ${RUN1}    ${RUN2}

# Targets:   FIRST  SECOND  THIRD
@{RUN1}      one    two     three
@{RUN2}      1      2       3

${FIRST}     ${None}  # Set by medusa:for
${SECOND}    ${None}  # Set by medusa:for
${THIRD}     ${None}  # Set by medusa:for
```
The above example suite will be executed twice:
* Once with `${FIRST}` = _one_, `${SECOND}` = _two_, `${THIRD}` = _three_
* Once with `${FIRST}` = _1_, `${SECOND}` = _2_, `${THIRD}` = _3_


Finally, you can also use a dictionary as an input variable with two target variables:
``` robot
*** Settings ***
Metadata    medusa:for    $DUT    $VAL    IN    $RUNS

*** Variables ***
# Targets:  DUT=VAL
&{DUTS}
...         foo=one
...         bar=two

${DUT}      ${None}  # Set by medusa:for
${VAL}      ${None}  # Set by medusa:for
```
The above example suite will be executed twice:
* Once with `${DUT}` = _foo_, `${VAL}` = _one_
* Once with `${DUT}` = _bar_, `${VAL}` = _two_


## `medusa:timeout` (optional)
The `medusa:timeout` metadata key can be used to set a suite-specific timeout. This timeout overrides the command-line option `-t`/`--timeout`. The value has the same format as the command-line option, see `medusa --help` for details.

Example:
``` robot
*** Settings ***
Metadata    medusa:timeout    300,60,5
```
This results in a soft timeout of 300 seconds, a hard timeout of 60 seconds and a kill timeout of 5 seconds.


## Complex example using all metadata
``` robot
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
```


# Contributing
INSYS icom does not provide support for Medusa.

## Reporting bugs
You can report bugs by opening an [issue](https://github.com/insys-icom/medusa/issues) in GitHub or sending an Email to [unicorn@regrow.earth](mailto:unicorn@regrow.earth).

## Adding features
If you want to contribute code to Medusa, you can either open a [pull request](https://github.com/insys-icom/medusa/pulls) or [send a patch via Email](https://git-send-email.io/) to [unicorn@regrow.earth](mailto:unicorn@regrow.earth).

A few things to note:
* Contributions should follow the coding style of the rest of Medusa (or feel free to explicitly suggest improvements to the coding style).
* Commits should have meaningful messages, naming the main file(s) that was/were changed and the change contents. If it's a bigger change, write more than just the first line. Example: `README: Fix missing comma` or `main: Add new command 'foo'`
* Use `make check`, `make fix`, `make format` and `make test` and fix any issues before submitting your contribution.
* When submitting a pull request or patch, ensure that your branch is up to date with the main branch.
* You need to verify that you hold the rights to contribute the code under the applicable license, depending on whether you are contributing code and/or documentation (see below).


# License
Medusa is open source software licensed under the [Apache License, Version 2.0](https://www.apache.org/licenses/LICENSE-2.0). Medusa documentation and other similar content use the [Creative Commons Attribution 4.0 International](https://creativecommons.org/licenses/by/4.0/) license.

See [LICENSE](./LICENSE) for the full Apache-2.0 license text and [NOTICE](./NOTICE) for the full copyright notice.
