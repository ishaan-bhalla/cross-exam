# Cross-Examine the Post Office
Voice-based cross-examination trainer built on real Post Office Horizon IT Inquiry witness statements.

## Setup
conda env create -f environment.yml -p ./venv
conda activate ./venv
pip install -e .
cp .env.example .env   # then add your AssemblyAI API key
