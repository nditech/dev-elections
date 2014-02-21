FROM ubuntu:precise

MAINTAINER Tim Akinbo <takinbo@timbaobjects.com>

RUN apt-get update

RUN apt-get install -y python-dev build-essential python-setuptools
RUN easy_install pip

ADD requirements.txt /app/
RUN pip install -r /app/requirements.txt

ADD README /app/
ADD apollo/ /app/apollo/
ADD doc/ /app/doc/
ADD Procfile.docker /app/Procfile

WORKDIR /app/
CMD ["start"]
ENTRYPOINT ["honcho"]

EXPOSE 5000
