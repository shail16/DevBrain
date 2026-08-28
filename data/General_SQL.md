# General Purpose SQL Queries and Functions Cheat Sheet

This document provides a quick reference for common SQL queries and functions used in data manipulation, retrieval, and analysis.

---

## 1. Basic Data Manipulation (CRUD)

### **SELECT** (Read)
Retrieve data from one or more tables.
```sql
-- Select specific columns
SELECT first_name, last_name, email 
FROM users;

-- Select all columns
SELECT * 
FROM users;

-- Select unique values
SELECT DISTINCT department 
FROM employees;
```

### **INSERT** (Create)
Add new rows to a table.
```sql
INSERT INTO customers (first_name, last_name, email)
VALUES ('John', 'Doe', 'john.doe@example.com');
```

### **UPDATE** (Update)
Modify existing data in a table.
```sql
UPDATE employees
SET salary = salary * 1.05
WHERE department = 'Sales';
```

### **DELETE** (Delete)
Remove rows from a table.
```sql
DELETE FROM orders
WHERE status = 'Cancelled';
```

---

## 2. Filtering and Sorting

### **WHERE** Clause
Filter records based on specific conditions.
```sql
SELECT * FROM products
WHERE category = 'Electronics' AND price > 500;
```

### **ORDER BY** Clause
Sort the result set ascending (ASC) or descending (DESC).
```sql
SELECT product_name, price
FROM products
ORDER BY price DESC;
```

### **LIMIT / TOP**
Restrict the number of rows returned.
```sql
-- PostgreSQL / MySQL / SQLite
SELECT * FROM users ORDER BY created_at DESC LIMIT 10;

-- SQL Server
SELECT TOP 10 * FROM users ORDER BY created_at DESC;
```

---

## 3. Joins
Combine rows from two or more tables based on a related column.

### **INNER JOIN**
Returns records that have matching values in both tables.
```sql
SELECT orders.order_id, customers.first_name
FROM orders
INNER JOIN customers ON orders.customer_id = customers.customer_id;
```

### **LEFT (OUTER) JOIN**
Returns all records from the left table, and matched records from the right table.
```sql
SELECT customers.customer_name, orders.order_id
FROM customers
LEFT JOIN orders ON customers.customer_id = orders.customer_id;
```

---

## 4. Aggregation and Grouping

### **GROUP BY & Aggregation Functions**
Group rows that have the same values into summary rows.
```sql
SELECT department, 
       COUNT(*) AS employee_count, 
       AVG(salary) AS avg_salary,
       MAX(salary) AS highest_salary
FROM employees
GROUP BY department;
```

### **HAVING** Clause
Filters grouped records (WHERE is for individual rows, HAVING is for groups).
```sql
SELECT department, COUNT(*) as employee_count
FROM employees
GROUP BY department
HAVING COUNT(*) > 5;
```

---

## 5. Common SQL Functions

### **String Functions**
*   `CONCAT(str1, str2)`: Adds two or more strings together.
*   `SUBSTRING(str, start, length)`: Extracts a substring.
*   `UPPER(str)` / `LOWER(str)`: Converts to uppercase/lowercase.
*   `REPLACE(str, from_str, to_str)`: Replaces occurrences of a substring.

```sql
SELECT CONCAT(first_name, ' ', last_name) AS full_name,
       UPPER(email) AS upper_email
FROM users;
```

### **Date Functions** (Syntax varies slightly by dialect)
*   `CURRENT_DATE()` or `GETDATE()`: Returns the current date/time.
*   `EXTRACT(part FROM date)` or `DATEPART()`: Extracts a part of a date (e.g., year, month).
*   `DATEDIFF(date1, date2)`: Difference between two dates.

```sql
SELECT order_id, 
       EXTRACT(YEAR FROM order_date) AS order_year
FROM orders
WHERE order_date >= CURRENT_DATE - INTERVAL '30 days'; -- PostgreSQL syntax
```

### **Control Flow (CASE WHEN)**
Used for conditional logic within queries.
```sql
SELECT employee_name, salary,
       CASE 
           WHEN salary >= 100000 THEN 'High'
           WHEN salary >= 50000 THEN 'Medium'
           ELSE 'Low'
       END AS salary_tier
FROM employees;
```

---

## 6. Window Functions
Perform calculations across a set of table rows that are related to the current row.

### **ROW_NUMBER, RANK, DENSE_RANK**
```sql
SELECT employee_name, department, salary,
       ROW_NUMBER() OVER(PARTITION BY department ORDER BY salary DESC) as row_num,
       RANK() OVER(PARTITION BY department ORDER BY salary DESC) as rank
FROM employees;
```
