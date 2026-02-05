# SLOPE Actuarial Scripts Library
This Library contains sets of projects and workflows that have been built for various client needs.

Each subfolder represents a separate project or workflow for a specific purpose.
The `Shared` folder contains common utility classes or functions that are used by multiple projects.
When using specific project folders, you may need to include the `Shared` folder contents as well for the project to work.
See `PBR_Solver/README.md` for details on the VM-20 PBR solver workflow.

## VS Code Extensions
This project was built to work with VS Code and utilizes the following extensions:
1. Python
2. Python Debugger

## Install uv if not already installed
### Windows
```
winget install --id=astral-sh.uv  -e
```
### MacOS
```
brew install uv
```

## Managing Dependencies
You can add dependencies to any individual project/module within this repo using the command line options for uv.

First, change directories into the specific project
```
cd {DiretoryName}
```

Then add each dependency using the `uv add` command:
```
uv add requests
uv add pandas
```
