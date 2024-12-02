<h1 align="center">
  <a href="https://www.ndi.org/"><img src="https://www.ndi.org/sites/all/themes/ndi/images/NDI_logo_svg.svg" alt="NDI Logo" width="200"></a>
</h1>

<h1 align="center">
  Apollo
</h1>

[![CircleCI](https://circleci.com/gh/nditech/dev-elections/tree/master.svg?style=svg&circle-token=d73aae2670476f167920a4494b6087a6f8ef49e9)](https://circleci.com/gh/nditech/dev-elections/tree/master)

  ### Table of Contents
  1. [Introduction](#introduction)
  1. [Install](#install)
  1. [Builld and Deploy](#build-and-deploy)
  1. [Web Server Configuration](#nginx-configuration)
  1. [Application Configuration Settings](#application-configuration-settings)
  1. [Legacy Installation Method](#legacy-installation-method)


## Apollo 3.x Deployment Guide

### Introduction

This document details the steps required in deploying a fully functional installation of Apollo 3. Apollo is a data management platform to support citizen election observation and other large-scale structured data collection efforts. Developed by Tim Akinbo and his team at TimbaObjects in conjunction with NDI’s Elections team, Apollo aids the management of observers, verification of collected information, and automated aggregation for analysis. Citizen watchdogs play a critical role in validating political processes, but to be convincing must back claims with data. Elections are one of the foundations of legitimate democracy when the official results truly represent the will of the voters. Systematic election observation requires large amounts of structured information from hundreds or thousands of observers and determining what it means – fast. Apollo aids the management of observers, verification of collected information, and automated aggregation for analysis.


### Install

#### Dependencies

The dependencies for building and deploying an Apollo 3 instance are:
* [Git](https://git-scm.com/book/en/v2/Getting-Started-Installing-Git) - for retrieving the source code from a source code versioning repository
* [Docker](https://docs.docker.com/install/linux/docker-ce/ubuntu/) - for building and deploying the Apollo 3 instance.
* [Docker-compose](https://docs.docker.com/compose/install/) - also for building and deploying Apollo 3. (If need be, it is possible to deploy Apollo without this dependency using the legacy instructions for deploying found here: [Legacy Installation Method](#legacy-installation-method))
* [NGINX](https://www.nginx.com/resources/wiki/start/topics/tutorials/install/) - Apollo uses Nginx as a web-server for hosting the site.

#### Obtaining the Source

After installing git, you will be able to clone the current version of Apollo from this repo, using:

```
git clone https://github.com/nditech/apollo.git
```

After downloading, configure a settings file in the main folder called `settings.ini`. A basic sample settings file is shown below. The install will not work if a settings file is not created with a Secret Key specified. The secret key should be any randomly generated string of characters of similar length to the example provided. For more information on additional configuration settings, read the section "[Application Configuration Settings](#application-configuration-settings)" below.

```
[settings]
SECRET_KEY=sD2av35FAg43rfsDa
PREFERRED_URL_SCHEME=http
```

### Build and Deploy

In order to simplify the deployment process, Apollo now includes a docker compose application configuration to allow admins skip the entire process of having to start the database, task queue, worker and main application containers individually.

To build and start the application, simply run (from within the main folder):

`sudo docker-compose up -d`

The main application container and worker containers will be built and run together with the supporting database and task queue containers. After running the command initially, subsequent builds will use cached container images. To see which containers are running, use `sudo docker ps`.

To deploy the code from scratch without drawing upon the cached images (for example to incorporate subsequent any changes made to the Apollo code outside of the docker containers), run `docker-compose build --no-cache`.

To build and install on a Mac, see the additional instructions in the section titled _Installing on Macs_.

### Nginx configuration

After deploying the containers, the Apollo application can be accessed by configuring Nginx. After installing nginx on your server, become the root user and go to `/etc/nginx/`. Remove all files from the directories `sites-available` and `sites-enabled`. Create a blank file called `apollo` in `sites-available`, and create a symbolic link to a file in `sites-enabled` by running the command below:

`ln -s /etc/nginx/sites-available/apollo /etc/nginx/sites-enabled/apollo`

Then edit the apollo file in `sites-available` with Nginx configurations. A sample file is shown below. Replace INSERT_SITE_URL with the url for your site (replace INSERT_SITE_URL in both places). Insert your ssl crt at /etc/ssl/certs/ssl.crt and your ssl key at /etc/ssl/certs/ssl.key.

```
server {
    listen 80;
    server_name INSERT_SITE_URL;
    location / {
        rewrite ^ https://$server_name$request_uri? permanent;
    }
}

server {
    listen 443;
    server_name INSERT_SITE_URL;
    #limit_req zone=one burst=10 nodelay;
    #limit_req_status 429;
    ssl_trusted_certificate /etc/ssl/certs/ssl.crt;

    location / {
        proxy_pass http://localhost:5000;
        proxy_redirect http:// $scheme://;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Server $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        port_in_redirect off;
        proxy_connect_timeout 300;
        proxy_read_timeout 180;
    }

    ssl on;
    ssl_certificate /etc/ssl/certs/ssl.crt;
    ssl_certificate_key /etc/ssl/certs/ssl.key;
    ssl_session_cache    shared:SSL:10m;
    ssl_session_timeout  10m;
    ssl_protocols TLSv1 TLSv1.1 TLSv1.2;
    ssl_prefer_server_ciphers on;
    ssl_ciphers "EECDH+ECDSA+AESGCM EECDH+aRSA+AESGCM EECDH+ECDSA+SHA384 EECDH+ECDSA+SHA256 EECDH+aRSA+SHA384 EECDH+aRSA+SHA256 EECDH+aRSA+RC4 EECDH EDH+aRSA RC4 !aNULL !eNULL !LOW !3DES !MD5 !EXP !PSK !SRP !DSS +RC4 RC4";


    gzip on;
    gzip_http_version 1.1;
    gzip_vary on;
    gzip_comp_level 6;
    gzip_proxied any;
    gzip_types text/plain text/css application/json application/x-javascript text/xml application/xml application/xml+rss text/javascript;

    # make sure gzip does not lose large gzipped js or css files
    # see http://blog.leetsoft.com/2007/7/25/nginx-gzip-ssl
    gzip_buffers 16 8k;

    # Disable gzip for certain browsers.
    gzip_disable "MSIE [1-6].(?!.*SV1)";
}
```

After changing the file, restart nginx using `service nginx restart` (or `service nginx start` if it has not yet been started).

#### Logging in

You should now be able to log in to your site by navigating to the IP or URL for your server, or if you are running it locally without nginx by going to port 5000 on your localhost (http://localhost:5000). The default login is username: `admin` / password: `admin`. This needs to be changed upon logging in.

### Application Configuration Settings

Each deployment installation can be further customized by modifying the contents of the `settings.ini` file. The syntax is for each setting to use the header listed below with an equal sign and then its value, for example `PREFERRED_URL_SCHEME=https` or `TIMEZONE=Africa/Lagos`. Here are a collection of settings parameters and sample values together with an explanation of what they do.

**SECRET_KEY**
(e.g. LBZyd8EY80mALqb7bl8o3da8)

The secret key contains a random string of characters, numbers and symbols and is used internally for signing and encrypting cookies and other security-related tokens. Best security practice requires that you set the value to a random value before starting the containers. Note that if this value is changed after the application is already fully configured, then user logins will stop working as the application would not be able to decrypt stored passwords. So this should remain the same throughout the lifetime of the application. This is a compulsory configuration option.


**PREFERRED_URL_SCHEME**
(e.g. http or https)

This parameter determines whether the application server will explicitly check if all requests are being served over a secure (https) connection. By setting this value to https, you effectively turn this check on and all connections must be secure before access is granted.


**TIMEZONE**
(e.g. Africa/Lagos)

The timezone parameter configures the timezone that the application server uses by default. Usually this is set to the timezone of the country for which the application instance is deployed. For a full list of support timezone values, please visit this [wikipedia article](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones).


**GOOGLE_TAG_MANAGER**
(e.g. GTM-1234567)


For digital marketing or product improvement initiatives you may want to include JavaScript and or HTML snippet tags for tracking and analytics on Apollo.

If you need to manage tags that are inserted into the application, one way to do so it to use the Google Tag Manager. This parameter allows you to set the Google Tag Manager code that is linked to the Google account from where the tags will be managed. To enable Tag Manager, modify the `settings-docker.ini` file. Add the configuration parameter `GOOGLE_TAG_MANAGER_KEY` and specify the **tag manager key** you get from Google and restart the container.

More information on using Google Tag Manager can be found [here](https://marketingplatform.google.com/about/tag-manager/), and [here](https://developers.google.com/tag-manager/).


**REDIS_DATABASE**
(e.g. 0)

This value determines the redis database that is used by the application. It takes the default value of 0 but (in the unlikely event that you need to share a redis installation) it can be changed to a different value. By default most redis installations have a maximum value of 15.


**REDIS_HOSTNAME**
(e.g. redis)

As was described above, if you require connecting to an external redis server, you can specify the value of the hostname for this redis database here.


**DATABASE_HOSTNAME**
(e.g. postgres)

As was the case in REDIS_HOSTNAME, there might be cases where an external PostgreSQL database is to be used, setting the DATABASE_HOSTNAME allows the application and worker applications to connect to this.


**DATABASE_NAME**
(e.g. apollo)

If you need to use the non-default database name for the installation, you can change the value here. Please note that if you are using an external PostgreSQL server, you may not need to create and link to the postgres container; in which case, simply providing the DATABASE_HOSTNAME and the DATABASE_NAME will be sufficient for the application containers to connect to the provided database.

You would also need to drop the DATABASE_NAME environment specification (-e DATABASE_NAME=...) when creating the application containers.


**DEBUG**
(e.g. False)

Debug information is usually only used when the application is being built or when an issue is being debugged. The default value of False is sufficient and should only be changed to True if there’s the need to debug the application. Setting DEBUG to True may reveal sensitive information to the user and should only be used when actual debugging is being done and reverted to the default value when debugging is over.

**MAIL_SERVER**
(e.g. smtp.sendgrid.net)
**MAIL_PORT**
(e.g. 587)
**MAIL_USE_TLS**
(e.g. True)
**MAIL_USERNAME**
(e.g. sendgrid-username)
**MAIL_PASSWORD**
(e.g sendgrid-apikey)

These set of configuration values allow the administrator to configure a mail server for the purpose of being able to send out transactional emails like password resets, notices on task completion, etc. It is highly recommended that these values are configured.


**PROMETHEUS_SECRET**
(e.g. pmsecretz)

Apollo 3 provides support for an external monitoring server (Prometheus) to be able to obtain application performance metrics. In order to randomize the URL from which the stats are retrieved from, the PROMETHEUS_SECRET is added as an additional URL fragment and must be correct for the metrics to be provided. The metrics url becomes https://apollo3servername/metrics/{PROMETHEUS_SECRET}.


**MAPBOX_TOKEN**
(e.g. pk.eyJ1I.BOh_5nWoa-sWP)

If you would rather have the better looking MapBox base tiles for the maps, you should add your MapBox API token as a parameter to the settings file.


**ENABLE_SOCIAL_LOGIN**
(e.g. True)

If you would like to offer the option to login with either a Facebook or Google account, you should enable the social login plugin by setting this parameter to True.

**GOOGLE_CLIENT_ID**
**GOOGLE_CLIENT_SECRET**

To enable Google authentication, you'll need to create credentials for the Apollo application in your Google account (https://console.developers.google.com/apis/credentials). Copy the client id and secret and set them to the `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` respectively.

**FACEBOOK_CLIENT_ID**
**FACEBOOK_CLIENT_SECRET**

Similarly, to enable Facebook login, you'll need to obtain Facebook Login (https://developers.facebook.com/docs/facebook-login) credentials to use with Apollo. After setting the appropriate parameters, login with Facebook will be enabled.


**API_KEY**
(e.g. 3a;lk9243)

The `API_KEY` setting enables external API access to the artefacts exposed by Apollo. This parameter specifies the key that must accompany every API request to enable access.


### Legacy Installation Method

An older installation method is documented below. In almost all cases, using docker compose is preferable. However if for some reason this is not possible, use the method below instead in place of the section above labeled *Installation and Deployment*.

#### Building the Docker Images

Once the repository has been cloned, you build the application image by first changing the directory to the one containing the source code and running the command:

`docker build -t apollo .`

This will start the build process where all application dependencies are downloaded and installed and an application image (from which the containers will be created) will be built.
Running the Application Containers

There are two essential application containers that are required in every Apollo 3 deployment. The first one is the application container - this houses the main web application and serves the application contents to the web browsers and processes all user input.

The second is the long-running task worker and is responsible for handling tasks that take a much longer time to run and that may otherwise block the main web application process and possibly timeout while waiting for the task to complete.

Supporting containers include database container (PostgreSQL) and the task queue container (Redis).

Start out by first running the database and task queue containers:

`docker run -d -e POSTGRES_DB=${DATABASE_NAME:-apollo} -v postgres_data:/var/lib/postgresql/data -n postgres postgres:10-alpine`
`docker run -d -n redis redis:4-alpine`

Then you run the worker container:

`docker run -d -n worker --link postgres --link redis -e DATABASE_NAME=${DATABASE_NAME:-apollo} -v upload_data:/app/uploads -v settings.ini:/app/settings.ini apollo:latest ./manage.py worker`

Finally, you can then run the main application container:

`docker run -d -n web --link postgres --link redis -e DATABASE_NAME=${DATABASE_NAME:-apollo} -v upload_data:/app/uploads -v ./settings.ini:/app/settings.ini apollo:latest ./manage.py gunicorn -c gunicorn.conf`

In both instances, the database (PostgreSQL) and task queue (Redis) containers are linked to the worker and main application containers.


### Installing on Macs

#### Running on Apple Silicon

The `postgis` docker image used in this setup does not have a build for Apple Silicon, so you can receive errors such as these:

```
 ! postgres The requested image's platform (linux/amd64) does not match the detected host platform (linux/arm64/v8) and no specific platform was requested
```

A workaround is to create your own image locally. First, create a folder in the project named `postgres`, then a `Dockerfile` inside with the following content:

```
# Use the official PostgreSQL image that supports ARM64
FROM postgres:12

# Install PostGIS
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        postgis postgresql-12-postgis-3 \
        postgresql-12-postgis-3-scripts && \
    rm -rf /var/lib/apt/lists/*
```

Finally, in the `docker-compose.yml` file, replace the line `image: postgis/postgis:12-3.0` with `build: ./postgres`.

#### Port 5000 in Use

On certain MacOS versions you might receive the following error:

```
Error response from daemon: Ports are not available: exposing port TCP 0.0.0.0:5000 -> 0.0.0.0:0: listen tcp 0.0.0.0:5000: bind: address already in use
```

This can be caused by the Control Center listening on this port to support AirPlay, as described [here](https://stackoverflow.com/questions/72369320/why-always-something-is-running-at-port-5000-on-my-mac).

As a workaround, you can either follow the instructions [here](https://stackoverflow.com/questions/72369320/why-always-something-is-running-at-port-5000-on-my-mac) to disable the AirPlay Receiver, or you can update the port used by opening the `docker-compose.yml` file and changing the line `- "5000:5000"` to `- "5002:5000"`.
