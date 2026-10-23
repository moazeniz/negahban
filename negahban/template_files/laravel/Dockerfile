# Works for a real Laravel project (composer.json + artisan) and for the tiny starter below.
FROM composer:2 AS vendor
WORKDIR /app
COPY . .
RUN composer install --no-dev --no-interaction --prefer-dist --optimize-autoloader --ignore-platform-reqs --no-scripts

FROM php:8.3-apache
RUN docker-php-ext-install pdo_mysql opcache && a2enmod rewrite \
 && sed -ri 's/AllowOverride None/AllowOverride All/g' /etc/apache2/apache2.conf \
 && sed -ri 's!/var/www/html!/var/www/html/public!g' /etc/apache2/sites-available/*.conf \
 && sed -ri 's/Listen 80$/Listen {{port}}/' /etc/apache2/ports.conf \
 && sed -ri 's/:80>/:{{port}}>/' /etc/apache2/sites-available/000-default.conf
WORKDIR /var/www/html
COPY --from=vendor /app /var/www/html
RUN mkdir -p storage/framework/cache storage/framework/sessions storage/framework/views storage/logs bootstrap/cache \
 && chown -R www-data:www-data storage bootstrap/cache
EXPOSE {{port}}
CMD ["sh", "-c", "if [ -f artisan ]; then php artisan config:cache; php artisan route:cache || true; php artisan migrate --force || true; fi; exec apache2-foreground"]
