import os
from setuptools import setup, find_packages

def read(fname):
    return open(os.path.join(os.path.dirname(__file__), fname)).read()


setup(
    name = 'fbgp',
    version = '1.0.0',
    author = 'Trung Truong',
    author_email = 'trungdtbk@gmail.com',
    description = ("A software defined BGP router."),
    license = 'BSD',
    long_description = read('README.rst'),
    classifiers = [
        "Development Status :: 2 - Pre-Alpha",
        "Topic :: Networking :: Routing :: BGP",
        "Programming Language :: Python",
        "License :: BSD License",
        ],
    packages=find_packages(exclude=['tests']),
    entry_points = {
        'console_scripts': [
            'fbgp_exabgp_hook=fbgp.exabgp_hook:main',
            ]
        },
    install_requires=[
            'pyyaml',
            'ryu',
            'oslo.config',
            'twisted'
            ]
    )
