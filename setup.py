import setuptools

with open("README.md", "rt", encoding="utf-8") as fh:
    long_description = fh.read()

setuptools.setup(
    name="purlovia",
    version="0.0.1",
    author="arkutils",
    author_email="author@example.com",
    description="Project Purlovia - digging up Ark data",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/arkutils/Purlovia",
    packages=setuptools.find_packages(),
    classifiers=[
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.10"
        "License :: OSI Approved :: GNU Lesser General Public License v3 (LGPLv3)",
        "Operating System :: OS Independent",
    ],
    python_requires='>=3.10',
    install_requires=[
        "requests>=2.27.0",
        "pydantic>=1.7.4",
        "pyyaml>=5.3",
        "psutil>=5.9.0",
    ],
)
