from setuptools import setup, find_packages

author = 'Florian R. Hölzlwimmer, David S. Fischer'

setup(
    name='diffxpy',
    author=author,
    author_email='florian.hoelzlwimmer@helmholtz-muenchen.de',
    packages=find_packages(),
    install_requires=[
        'numpy',
        'scipy',
        'patsy',
        'sgdglm',
    ],
    extras_require={
        'optional': [
            'xarray',
            'anndata',
        ]
        # 'scanpy_deps': [
        #     "scanpy",
        #     "anndata"
        # ],
        # 'plotting_deps': [
        #     "plotnine",
        #     "matplotlib"
        # ]
    }

)
