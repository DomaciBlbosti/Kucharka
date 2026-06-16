-- Referenční schéma (aplikace si tabulky vytvoří sama přes ORM při startu).
-- Tady jen pro přehled / ruční inspekci.

CREATE TABLE ingredient (
  id            INT AUTO_INCREMENT PRIMARY KEY,
  name_cs       VARCHAR(200) NOT NULL,
  name_en       VARCHAR(200),
  code          VARCHAR(50),
  category      VARCHAR(120),
  kcal_100g     FLOAT, protein_100g FLOAT, carbs_100g FLOAT,
  fat_100g      FLOAT, fiber_100g FLOAT,
  density       FLOAT,                      -- g na 1 ml
  source        VARCHAR(60),
  INDEX (name_cs), INDEX (code)
);

CREATE TABLE ingredient_alias (
  id            INT AUTO_INCREMENT PRIMARY KEY,
  alias         VARCHAR(200) NOT NULL UNIQUE,
  ingredient_id INT NOT NULL,
  FOREIGN KEY (ingredient_id) REFERENCES ingredient(id) ON DELETE CASCADE
);

CREATE TABLE recipe (
  id              INT AUTO_INCREMENT PRIMARY KEY,
  title           VARCHAR(300) NOT NULL,
  source_url      VARCHAR(600) NOT NULL UNIQUE,
  source_domain   VARCHAR(160),
  image_url       VARCHAR(600),
  video_url       VARCHAR(600),
  instructions    TEXT,
  servings        INT, total_time INT,
  rating          FLOAT, rating_count INT,
  category        VARCHAR(160),
  kcal_per_serving FLOAT,
  raw_json        TEXT,
  created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
  INDEX (title), INDEX (source_domain)
);

CREATE TABLE recipe_ingredient (
  id            INT AUTO_INCREMENT PRIMARY KEY,
  recipe_id     INT NOT NULL,
  raw_text      VARCHAR(400) NOT NULL,
  ingredient_id INT,
  amount        FLOAT, unit VARCHAR(40), grams FLOAT, kcal FLOAT,
  optional      BOOLEAN DEFAULT 0,
  FOREIGN KEY (recipe_id) REFERENCES recipe(id) ON DELETE CASCADE,
  FOREIGN KEY (ingredient_id) REFERENCES ingredient(id)
);

CREATE TABLE pantry_item (
  id            INT AUTO_INCREMENT PRIMARY KEY,
  ingredient_id INT NOT NULL UNIQUE,
  amount        FLOAT, unit VARCHAR(40),
  updated_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (ingredient_id) REFERENCES ingredient(id) ON DELETE CASCADE
);

CREATE TABLE shopping_item (
  id            INT AUTO_INCREMENT PRIMARY KEY,
  label         VARCHAR(200) NOT NULL,
  ingredient_id INT,
  checked       BOOLEAN DEFAULT 0,
  created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (ingredient_id) REFERENCES ingredient(id)
);
