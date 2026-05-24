from setuptools import setup

with open("README.md", "r", encoding="utf-8") as f:
    long_description = f.read()

setup(
    name="pymemforce",
    version="2.1.0",
    author="PyMemForce Team",
    description="赋予 Python 强制内存控制的能力 - 让 Python 拥有类似 C++ 的内存管理",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/1234567740/PyMemForce",
    py_modules=["PyMemForce"],
    classifiers=[
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.7",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "License :: OSI Approved :: GNU General Public License v3 (GPLv3)",
        "Operating System :: OS Independent",
        "Topic :: Software Development :: Libraries :: Python Modules",
        "Topic :: System :: Memory Management",
    ],
    python_requires=">=3.7",
    license="GPL-3.0",
)
